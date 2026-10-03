"""Pure permission and decision-retention oracles with isolated owner boundaries."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import Workspace, draft_contracts, execution_session
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.research_session_contracts import STEP_V2
from evidence_wiki.run_policy import RunPolicy
from tests.test_run_policy_scope import DIGEST, native_policy_document, owner_observation
from tests.test_workflow_contracts import job as reference_job


@contextmanager
def open_gate(*_args, **_kwargs):
    yield


def isolated_boundaries(monkeypatch, policy, *, steps_used=0, steps_limit=8):
    records, finished, closed, reservations = [], [], [], []
    now = datetime.now(timezone.utc)
    document = {"state": {"current": "answering"}, "started_at": now.isoformat(), "updated_at": now.isoformat()}
    originals = [{"original_id": identifier, "original_text": "Frozen original " + str(index),
                  "questions": [{"slug": "first" if index == 1 else "second", "question_id": identifier}],
                  "missing_question_ids": [], "accepted": False}
                 for index, identifier in enumerate(policy.to_dict()["scope"]["question_ids"], 1)]
    view = {"basis_id": DIGEST, "policy_id": policy.policy_id, "policy_compatible": True,
            "accounting": {"original_ids": [row["original_id"] for row in originals], "original_outcomes": originals,
                           "accounting_complete": True, "missing_mapping": False, "accepted_count": 0,
                           "unresolved_count": len(originals)},
            "qualification": {"state": "qualified", "reason": "controlled_owner", "profiles": []},
            "canonical_release": {"publication": None}, "authority": {"terminal_grants_available": True}, "usage": {**owner_observation(policy)["spent"], "claims": 0},
            "execution": {"steps_used": steps_used, "steps_limit": steps_limit, "pending": None,
                          "accounting": {"overrun": [], "usage_unknown": False},
                          "source_bindings": [{"source_id": "captured-source", "extent_ref": "/workspace/raw/measurements.txt", "closure_id": DIGEST}]}}
    session = SimpleNamespace(policy=policy, run_id="controlled-run", root="/workspace", binding={},
                              context=SimpleNamespace(require_grant=lambda _grant: None),
                              execution=SimpleNamespace(profile=SimpleNamespace(total_steps=steps_limit), ledger=lambda *_args: None))

    def current(_session):
        result = copy.deepcopy(view)
        result["execution"]["steps_used"] += sum(row["data"].get("consumes_step", False) for row in records)
        return result

    def append(_session, kind, request_id, data):
        event_ref = "event-" + str(len(records))
        records.append({"kind": kind, "request_id": request_id, "event_ref": event_ref, "data": copy.deepcopy(data)})
        return {"event_id": event_ref}

    def finish(_session, decision):
        finished.append(copy.deepcopy(decision))
        document["state"]["current"] = "complete" if decision["outcome"] == "accepted" else "no_ship"
        return copy.deepcopy(document)

    monkeypatch.setattr(execution_session, "gate", open_gate)
    monkeypatch.setattr(execution_session, "retained", lambda _session: records)
    monkeypatch.setattr(execution_session, "append", append)
    monkeypatch.setattr(execution_session, "current", current)
    monkeypatch.setattr(execution_session, "_document", lambda _session: copy.deepcopy(document))
    monkeypatch.setattr(execution_session, "_begin", lambda _session: copy.deepcopy(document))
    monkeypatch.setattr(execution_session, "_finish", finish)
    monkeypatch.setattr(execution_session, "_close", lambda _session, step: closed.append(copy.deepcopy(step)))
    return SimpleNamespace(session=session, records=records, view=view, finished=finished, closed=closed,
                           current=current, append=append, reservations=reservations)


def accepted_observation(policy, seconds):
    value = owner_observation(policy)
    second = copy.deepcopy(value["selected_outcomes"][0])
    second.update(question_id="original-two", outcome_ref="second-result")
    value["selected_outcomes"].append(second)
    value.update(accepted_count=2, unresolved_count=0, basis_id=DIGEST, expected_basis_id=DIGEST)
    for row in value["selected_outcomes"]:
        row["basis_id"] = DIGEST
    value["spent"]["seconds"] = seconds
    assert policy.decide(value)["outcome"] == "accepted"
    return value


def test_fresh_accepted_replay_is_retained_before_finish_and_anchors_closure(monkeypatch):
    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    old_input = accepted_observation(policy, 1)
    fresh_input = accepted_observation(policy, 2)
    original, fresh = policy.decide(old_input), policy.decide(fresh_input)
    isolated.view["accounting"].update(accepted_count=2, unresolved_count=0)
    for row in isolated.view["accounting"]["original_outcomes"]:
        row["accepted"] = True
    isolated.view["usage"] = copy.deepcopy(fresh_input["spent"])
    isolated.view["qualification"]["profiles"] = copy.deepcopy(fresh_input["evaluators"])
    assert fresh != original
    step = {"schema_version": STEP_V2, "request_id": "replay", "run_id": isolated.session.run_id,
            "binding_id": DIGEST, "before_basis_id": DIGEST, "outcome": "accepted", "reason": "current_outcomes",
            "terminal": True, "decision": original, "job": None, "event_ref": None, "canonical_run_state": "answering",
            "accounting": copy.deepcopy(isolated.view["accounting"]), "historical": False, "action": "release",
            "attempt_id": None, "authorized": False, "evidence_accepted": False}
    isolated.append(isolated.session, "execution_step", "retained-release", {"step": step, "decision_input": old_input})
    owner = execution_session.owner
    monkeypatch.setattr(execution_session, "owner", lambda name: SimpleNamespace(TERMINAL_STATES={"complete", "no_ship"})
                        if name == "run_controller" else owner(name))
    monkeypatch.setattr(execution_session, "policy_observation", lambda *_args: copy.deepcopy(fresh_input))
    finish = execution_session._finish

    def require_retained_decision(session, decision):
        assert any(row["kind"] == "execution_step" and row["data"]["step"]["decision"] == decision
                   and row["data"].get("decision_input") == fresh_input for row in isolated.records)
        return finish(session, decision)

    monkeypatch.setattr(execution_session, "_finish", require_retained_decision)
    execution_session.advance(isolated.session, request_id="replay", expected_basis_id=DIGEST)
    assert isolated.finished == [fresh]
    assert isolated.closed[-1]["decision"] == fresh


def closed_proposal(original_id, original_text, claim_count):
    claims = [{"id": "claim-" + str(index), "question_slug": "first", "qualification": "supported",
               "text": "Observed local value " + str(index), "scope": "Declared local observations.",
               "time": "Captured observation.", "units": "Unitless.", "premises": [], "derivation": None,
               "limitations": ["Independent semantic review remains required."],
               "evidence": [{"source_id": "captured-source", "record_sha256": DIGEST,
                             "observed_at": "2026-10-03T00:00:00Z", "capture": "primary", "quote": "Observed local value.",
                             "location_hint": None, "anchor": None}]} for index in range(claim_count)]
    result = {"schema_version": draft_contracts.PROPOSAL,
              "originals": [{"original_id": original_id, "text": original_text, "question_slugs": ["first"]}],
              "inventory": {"schema_version": "evidence-strict-claims/v1",
                            "questions": [{"slug": "first", "original_id": original_id, "question": original_text.strip()}],
                            "claims": claims}}
    return draft_contracts.validate_document(draft_contracts.PROPOSAL, result)


@pytest.mark.parametrize("claim_limit,claim_count,permitted", [(1, 2, False), (2, 2, True)])
def test_proposed_inventory_claim_extent_is_checked_before_application_or_mutation(monkeypatch, claim_limit, claim_count, permitted):
    document = native_policy_document()
    document["budgets"]["claims"] = claim_limit
    policy = RunPolicy(document)
    isolated = isolated_boundaries(monkeypatch, policy)
    original = isolated.view["accounting"]["original_outcomes"][0]
    proposal = closed_proposal(original["original_id"], original["original_text"], claim_count)
    now = datetime.now(timezone.utc)
    job = reference_job()
    job.update(id="draft", policy_id=policy.policy_id, basis_id=DIGEST, revision_precondition=DIGEST,
               original_text=original["original_text"], scope=[original["original_id"]], payload={"question": original["original_text"]},
               issued_utc=now.isoformat(), deadline_utc=(now + timedelta(seconds=300)).isoformat())
    profile = ExecutionProfile("controlled-profile", 8, "EUR", "controlled-pricing", (
        ExecutionRole("researcher", "host_worker", "registered-controller", "controlled-model", "prompt/v1"),), 0, 5, 0, "0")
    attempt = ExecutionAttempt.create(attempt_id="draft", nonce="nonce", run_id=isolated.session.run_id,
                                      binding_id=DIGEST, profile=profile, config_ref="researcher", job=job, input_document={})
    observation = {"state": "succeeded", "final_accounting": True, "output": {"proposal": proposal}}
    isolated.append(isolated.session, "execution_attempt", "draft-intent", {
        "attempt": attempt.document(), "original_id": original["original_id"], "purpose": "generate"})
    isolated.append(isolated.session, "execution_observation", "draft-result", {"attempt_id": "draft", "observation": observation})
    isolated.session.execution.attempt = lambda _document: attempt
    isolated.session.execution.authenticate = lambda supplied, _attempt: copy.deepcopy(supplied)
    monkeypatch.setattr(execution_session, "ResearchObservation", lambda *_args, **_kwargs:
                        SimpleNamespace(strict={"result": {"claims": []}}, finish=lambda: None))
    mutations = []
    monkeypatch.setattr(execution_session, "_mutation", lambda _session, identifier, operation, arguments, **_kwargs:
                        mutations.append((identifier, operation, copy.deepcopy(arguments))))
    if permitted:
        execution_session._apply(isolated.session, "draft")
        assert len(mutations) == 1 and mutations[0][1] == "strict.admit"
    else:
        with pytest.raises(EvidenceWikiError):
            execution_session._apply(isolated.session, "draft")
        assert mutations == []
        assert not any(row["kind"] == "execution_application" for row in isolated.records)


@pytest.mark.parametrize("used_steps,permitted", [(1, False), (0, True)])
def test_manual_job_reservation_obeys_and_charges_the_same_step_allowance(monkeypatch, used_steps, permitted):
    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy, steps_used=used_steps, steps_limit=1)
    payload = {"question": isolated.view["accounting"]["original_outcomes"][0]["original_text"]}
    monkeypatch.setattr(execution_session, "_sources", lambda *_args: (
        [{"source_id": "captured-source"}], copy.deepcopy(isolated.view["execution"]["source_bindings"])))
    monkeypatch.setattr(execution_session, "ResearchObservation", lambda *_args, **_kwargs: SimpleNamespace(
        strict={"state": "present", "result": {"claims": []}}, policy=None,
        questions=[{"slug": "first", "status": "unanswered"}, {"slug": "second", "status": "unanswered"}],
        finish=lambda: None))

    def reserve(session, request_id, _action, _view):
        isolated.reservations.append(request_id)
        assert isolated.current(session)["execution"]["steps_used"] == used_steps + 1
        return SimpleNamespace(job={"id": request_id, "operation": "generate", "payload": payload})

    monkeypatch.setattr(execution_session, "reserve", reserve)
    if permitted:
        result = execution_session.host_job(isolated.session, request_id="manual", operation="generate", payload=payload)
        assert result["id"] == "manual"
        assert isolated.reservations == ["manual"]
    else:
        with pytest.raises(EvidenceWikiError):
            execution_session.host_job(isolated.session, request_id="manual", operation="generate", payload=payload)
        assert isolated.reservations == []


def test_failed_cancellation_authentication_cannot_prevent_terminal_persistence(monkeypatch):
    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    isolated.session.context.grants = {"research_session.cancel_pending"}
    isolated.session.execution.attempt = lambda document: SimpleNamespace(attempt_id=document["attempt_id"], job=document["job"])
    attempt = {"attempt_id": "pending", "job": {"id": "pending-job",
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()}}
    isolated.append(isolated.session, "execution_attempt", "pending-intent", {"attempt": attempt})
    isolated.append(isolated.session, "execution_observation", "pending-start", {"attempt_id": "pending",
        "observation": {"state": "running", "final_accounting": False, "cancellation_confirmed": False}})
    monkeypatch.setattr(execution_session, "_action", lambda session, view, _records, **_kwargs:
                        execution_session._terminal_action(session, view, "stop_budget", "usage_unknown"))
    monkeypatch.setattr(execution_session, "_external", lambda *_args, **_kwargs: ({"forged": True}, None))

    def reject_observation(*_args, **_kwargs):
        execution_session.refuse("EXECUTION_OBSERVATION_UNAUTHENTICATED")

    monkeypatch.setattr(execution_session, "intake", reject_observation)
    result = execution_session.advance(isolated.session, request_id="stop", expected_basis_id=DIGEST)
    assert result["terminal"] and result["outcome"] == "stop_budget"
    assert any(row["kind"] == "execution_step" and row["data"]["step"]["terminal"] for row in isolated.records)
    pending = execution_session._pending(isolated.records)
    assert pending["cancellation_requested"] and not pending["cancellation_confirmed"]
    assert pending["reservation_occupied"]
    assert any(row["kind"] == "execution_host_unknown" for row in isolated.records)


@pytest.mark.parametrize("operation", ["question_claim.run_claim", "question_resolve.run_answer"])
@pytest.mark.parametrize("matching", [True, False])
def test_question_recovery_requires_exact_current_owned_postconditions(monkeypatch, operation, matching):
    from pathlib import Path

    from evidence_wiki import research_session

    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    isolated.session.root = Path("/workspace")
    isolated.session.context.actor_ref = "caller"
    isolated.session.binding = {"request_binding": DIGEST}
    grounding = [{"source_id": "captured-source", "quote": "Exact local observation."}]
    arguments = {"slug": "first", "agent_id": "caller"} if operation == "question_claim.run_claim" else {
        "slug": "first", "grounding": grounding, "source_ids": ["captured-source"], "strict_basis_id": DIGEST}
    frontmatter = {"status": "in_progress", "claimed_by": "caller"} if operation == "question_claim.run_claim" else {
        "status": "answered", "answered_by": "caller", "answer_page": "first.md", "grounding": grounding,
        "source_ids": ["captured-source"], "coverage_required": True, "grounding_required": True}
    if not matching:
        if operation == "question_claim.run_claim":
            frontmatter["claimed_by"] = "different-controller"
        else:
            frontmatter["grounding"] = [{"source_id": "captured-source", "quote": "Different observation."}]
    capture = SimpleNamespace(files={"research.yml": b"wiki:\n  root: wiki\n"}, revision_id=DIGEST)
    owners = {
        "_evidence_revision": SimpleNamespace(capture_workspace=lambda _root: copy.deepcopy(capture)),
        "question_status": SimpleNamespace(questions_directory=lambda *_args: Path("/workspace/wiki/questions"),
                                           load_frontmatter=lambda _page: copy.deepcopy(frontmatter)),
        "_caller_context": SimpleNamespace(original_questions=lambda *_args: {"gaps": [],
            "expected": {"original-one": "first"}, "originals": [{"id": "original-one", "question_ids": ["original-one"]}]}),
        "_strict_evidence": SimpleNamespace(resolve_policy=lambda *_args: {}, selection_inputs=lambda *_args: (
            None, {"claims": [{"question_slug": "first", "evidence": [{"source_id": "captured-source"}]}]}, DIGEST),
            grounding_entry=lambda *_args: copy.deepcopy(grounding[0])),
    }
    monkeypatch.setattr(execution_session, "owner", lambda name: owners[name])
    monkeypatch.setattr(research_session, "_request_binding", lambda _root: DIGEST)
    control = SimpleNamespace(checkpoint=lambda _name: None)
    if matching:
        result = execution_session._recover_question(isolated.session, operation, arguments, control)
        assert result["status"] == "reconciled" and result["historical"]
        assert not result["authorized"] and not result["evidence_accepted"]
    else:
        with pytest.raises(EvidenceWikiError):
            execution_session._recover_question(isolated.session, operation, arguments, control)


@pytest.mark.parametrize("committed", [True, False])
def test_expired_native_history_never_prepares_submits_or_completes_unfinished_work(monkeypatch, committed):
    from pathlib import Path

    from evidence_wiki import execution_accounting, mutation_contracts, mutation_operations
    from evidence_wiki.research_execution import ResearchExecution
    from tests.test_execution_accounting import reserved

    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    isolated.session.root = Path("/workspace")
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(execution_session, "_document", lambda _session: {
        "state": {"current": "answering"}, "started_at": (now - timedelta(seconds=200)).isoformat(),
        "updated_at": (now - timedelta(seconds=100)).isoformat()})
    identifier = "retained-effect"
    original = isolated.view["accounting"]["original_outcomes"][0]
    arguments = {"proposal": closed_proposal(original["original_id"], original["original_text"], 1)}
    reservation = {"attempt_id": identifier, "job_id": identifier, "controller_ref": "session-observer",
        "issued_at": (now - timedelta(seconds=150)).isoformat(),
        "reservation": {"seconds": "50", "model_calls": 0, "tool_calls": 1, "tokens": 0, "cost": "0"},
        "cost_unit": {"currency": "EUR", "pricing_ref": "controlled-pricing"}, "required_slots": ["dispatch", "result", "usage"]}
    isolated.append(isolated.session, "execution_native", "retained-intent", {
        "operation": "strict.admit", "arguments": arguments, "reservation": reservation})
    receipt = mutation_contracts.checked(mutation_contracts.RECEIPT, {
        "schema_version": mutation_contracts.RECEIPT, "request_id": identifier, "input_sha256": DIGEST,
        "operation": "strict.admit", "status": "committed", "before_revision": DIGEST, "after_revision": DIGEST,
        "result": {}, "error": None, "historical": True, "authorized": False, "evidence_accepted": False})
    existing = {"intent": {"operation": "strict.admit", "arguments": arguments}, "receipt": receipt if committed else None}
    active_calls = []

    def forbidden_effect(*_args, **_kwargs):
        active_calls.append(True)
        pytest.fail("Historical reconciliation attempted a new or unfinished effect.")

    mutations = SimpleNamespace(inspect=lambda _identifier: {"record": copy.deepcopy(existing)},
                                prepare=forbidden_effect, submit=forbidden_effect, reconcile=forbidden_effect)

    @contextmanager
    def workspace(*_args, **_kwargs):
        yield SimpleNamespace(mutations=mutations)

    monkeypatch.setattr(Workspace, "open", workspace)
    plan = reserved()
    isolated.session.execution.ledger = lambda *_args: copy.deepcopy(plan)
    isolated.session.execution.profile.cleanup_timeout_seconds = 0.2
    observer = object.__new__(ResearchExecution)
    observer._observer_key = b"o" * 32
    observer._profile = SimpleNamespace(currency="EUR", pricing_ref="controlled-pricing")
    isolated.session.execution.native_observation = observer.native_observation
    monkeypatch.setattr(mutation_operations, "_capture", lambda _root: {"revision_id": DIGEST})
    owner = execution_session.owner
    monkeypatch.setattr(execution_session, "owner", lambda name: SimpleNamespace(
        capture_workspace=lambda _root: SimpleNamespace(files={}), content_id=lambda *_args: DIGEST)
        if name == "_evidence_revision" else owner(name))
    execution_session._recover_history(isolated.session)
    assert active_calls == []
    observations = [row for row in isolated.records if row["kind"] == "execution_native_observation"]
    if committed:
        assert len(observations) == 1
        execution_accounting.validate_document(execution_accounting.OBSERVATION, observations[0]["data"]["observation"])
        assert observations[0]["data"]["active_execution_seconds"] is None
    else:
        assert observations == []
        assert any(row["kind"] == "execution_native_history_unknown" for row in isolated.records)
        assert execution_accounting.projection(plan)["usage_unknown"]


@pytest.mark.parametrize("attempt_id", [[], {}])
def test_malformed_intake_identity_has_a_typed_refusal_before_authentication(monkeypatch, attempt_id):
    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    with pytest.raises(EvidenceWikiError):
        execution_session.intake(isolated.session, {"attempt_id": attempt_id}, request_id="malformed")
    assert isolated.records == []


@pytest.mark.parametrize("operation", [[], {}])
def test_malformed_job_operation_has_a_typed_refusal_before_reservation(monkeypatch, operation):
    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    with pytest.raises(EvidenceWikiError):
        execution_session.host_job(isolated.session, request_id="malformed", operation=operation, payload={"question": "Frozen original 1"})
    assert isolated.records == []


def test_existing_job_replay_cannot_accept_conflicting_original_text(monkeypatch):
    from evidence_wiki.agent_execution import OBSERVATION as EXECUTION_OBSERVATION
    from evidence_wiki.workflow_contracts import JOB_V2, validate_document

    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    original = isolated.view["accounting"]["original_outcomes"][0]
    old = reference_job()
    old.update(schema_version=JOB_V2, id="retained", expected_result_schema=EXECUTION_OBSERVATION,
               original_text=original["original_text"], payload={"question": original["original_text"]})
    old = validate_document(JOB_V2, old)
    isolated.append(isolated.session, "execution_attempt", "retained-intent", {"attempt": {"job": old}})
    with pytest.raises(EvidenceWikiError):
        execution_session.host_job(isolated.session, request_id="retained", operation="generate",
                                   payload=old["payload"], original_text="Different frozen request.")
    assert len(isolated.records) == 1
