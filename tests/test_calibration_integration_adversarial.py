"""Independent registry/probe gate attacks; host doubles do not prove live models."""

import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import calibration_probes as probes
from evidence_wiki import execution_session as driver
from evidence_wiki.agent_execution import ExecutionAttempt, ExecutionProfile, ExecutionRole, _digest
from evidence_wiki.calibration_registry import CalibratedEvaluators, CalibrationPlan, CalibrationSelection
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.finite_domain import DOMAIN, observe_domain
from evidence_wiki.qualification_calibration import CalibrationAuthority, calibration_suite, digest
from evidence_wiki.reference_oracles import protected_cases
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3, WorkflowContractError
from tests.test_finite_domain import inputs as domain_inputs
from tests.test_qualification_calibration import EXPIRY, NOW, AuthenticatedHost
from tests.test_run_policy import policy_document
from tests.test_workflow_contracts import job


@pytest.fixture(scope="module")
def candidate():
    contexts = domain_inputs()
    rubric = contexts["rubric"]
    suite = calibration_suite(protected_cases(), suite_ref="finite-protected", split_ref="held-out", domains=[DOMAIN],
        rubric=rubric, model_settings={"temperature": "0", "seed": 1})
    selection = CalibrationSelection("profile-a", suite, model_revision="exact-model-revision")
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("worker", "researcher", ("model",)),
        ExecutionRoleScope("profile-a", "evaluator", ("model",))))
    profile = ExecutionProfile("calibrated-host", 1000, "EUR", "host-price", (
        ExecutionRole("worker", "host_worker", "generator", "generator-model", "prompt/v1"),
        ExecutionRole("profile-a", "evaluator", "controller-a", "reference-model", "prompt/v1")),
        1, 0, 1, "0", 1, role_scopes=scopes, calibration=CalibrationPlan((selection,)))
    policy = RunPolicy(policy_document())
    host = AuthenticatedHost(profile, suite)
    selected = selection.document()
    host.configuration.update(domains=[DOMAIN], model_revision=selected["model_revision"],
        reference_assessment_ref=selected["reference_assessment"]["assessment_ref"],
        domain_descriptor_sha256=digest(selected["domain_descriptor"]), active=True)
    observations = []
    for index, case in enumerate(suite["cases"]):
        selected_job = job("evaluate") | {"schema_version": JOB_V3, "id": f"reference-job-{index}",
            "controller_ref": "controller-a", "policy_id": policy.policy_id, "scope": policy.to_dict()["scope"]["question_ids"],
            "issued_utc": "2026-10-01T11:59:40Z", "deadline_utc": "2026-10-01T12:00:10Z",
            "task_role": "evaluator", "role_scope_sha256": scopes.scope("profile-a").sha256,
            "context_id": f"reference-context-{index}", "packet_sha256": _digest(case["input_document"]),
            "required_capabilities": ["model"], "expected_result_schema": EXECUTION_OBSERVATION,
            "budgets": profile.document()["job_budgets"]}
        attempt = ExecutionAttempt.create(attempt_id=f"reference-attempt-{index}", nonce=f"reference-nonce-{index}",
            run_id="calibration", binding_id="protected-binding", profile=profile, config_ref="profile-a", job=selected_job,
            input_document=case["input_document"])
        observations.append((attempt, host.receipt(attempt, case)))
    authority = CalibrationAuthority("calibration-issuer", b"q" * 32, "generator")
    record = authority.issue(policy, profile, "profile-a", suite, observations, host,
        observed_utc=NOW, expires_utc=EXPIRY, verification_seconds=30)
    assert record["state"] == "qualified", record["reasons"]
    registry = CalibratedEvaluators([record], authority)
    domain = observe_domain(**contexts)
    assert domain["state"] == "observed"
    probe = {"schema_version": "research-qualification-probe/v1", "profile_sha256": profile.sha256,
        "registry_sha256": registry.sha256, "semantic_basis_id": domain["semantic_basis_id"], "observed_utc": NOW,
        "expires_utc": "2026-10-01T12:00:20Z", "configurations": {"profile-a": copy.deepcopy(host.configuration)}, "reasons": []}
    return SimpleNamespace(policy=policy, profile=profile, registry=registry, rubric=rubric, domain=domain, probe=probe,
        record=record, authority=authority, contexts=contexts)


def rows(candidate, **changes):
    arguments = {"domain": candidate.domain, "probe": candidate.probe, "rubric": candidate.rubric,
        "rubric_sha256": digest(candidate.rubric)} | changes
    return candidate.registry.observations(candidate.policy, candidate.profile, NOW, candidate.rubric["id"], **arguments)


def test_exact_current_registry_domain_and_host_configuration_qualify_only_controlled_protocol(candidate):
    result = rows(candidate)
    assert result[0]["qualified"] is True and result[0]["independent"] is True


@pytest.mark.parametrize("field", ["active", "model_revision", "model_ref", "model_settings_sha256", "rubric_sha256",
    "suite_sha256", "split_ref", "reference_assessment_ref", "domain_descriptor_sha256", "execution_kind", "controller_ref"])
def test_current_effective_configuration_or_withdrawal_change_cannot_reuse_calibration(candidate, field):
    probe = copy.deepcopy(candidate.probe)
    probe["configurations"]["profile-a"][field] = False if field == "active" else "changed"
    assert rows(candidate, probe=probe)[0]["qualified"] is None


@pytest.mark.parametrize("change", ["basis", "registry", "profile", "expired", "future", "reasons", "missing"])
def test_probe_correlation_freshness_and_current_basis_are_required(candidate, change):
    probe = copy.deepcopy(candidate.probe)
    if change == "basis":
        probe["semantic_basis_id"] = "sha256:" + "a" * 64
    elif change == "registry":
        probe["registry_sha256"] = "a" * 64
    elif change == "profile":
        probe["profile_sha256"] = "a" * 64
    elif change == "expired":
        probe["expires_utc"] = NOW
    elif change == "future":
        probe["observed_utc"] = "2026-10-01T12:00:01Z"
    elif change == "reasons":
        probe["reasons"] = ["host_wait_expired"]
    else:
        probe = None
    assert rows(candidate, probe=probe)[0]["qualified"] is None


@pytest.mark.parametrize("dimension", ["original", "claim", "rubric", "source", "closure"])
def test_canonical_semantic_change_invalidates_prior_probe(candidate, dimension):
    context = copy.deepcopy(candidate.contexts)
    if dimension == "original":
        context["originals"][0]["original_text"] = "Assess the exact declared finite observation for changed-cohort."
    elif dimension == "claim":
        context["strict_claims"][0]["text"] += " Universal claim."
    elif dimension == "rubric":
        context["rubric"]["criteria"]["support"] += " Changed criterion."
    elif dimension == "source":
        context["sources"][0]["content"] += "\n"
    else:
        context["sources"][0]["source_binding"]["closure_id"] = "sha256:" + "a" * 64
    current = observe_domain(**context)
    assert rows(candidate, domain=current)[0]["qualified"] is None


@contextmanager
def unlocked_gate(counter):
    counter[0] += 1
    try:
        yield
    finally:
        counter[0] -= 1


def refresh_boundaries(monkeypatch, candidate, *, behavior="normal"):
    counter, recorded, calls = [0], [], []
    domain = copy.deepcopy(candidate.domain)
    session = SimpleNamespace(execution=SimpleNamespace(profile=candidate.profile, qualifications=candidate.registry),
        root="/workspace", context=SimpleNamespace(require_grant=lambda *_: None))
    def service(request):
        calls.append(copy.deepcopy(request))
        assert counter[0] == 0, "External qualification callback ran inside canonical coordination gate."
        returned = {"request": copy.deepcopy(request), "configuration": copy.deepcopy(candidate.probe["configurations"]["profile-a"])}
        if behavior == "nonce":
            returned["request"]["nonce"] = "replayed-nonce"
        if behavior == "basis":
            returned["request"]["semantic_basis_id"] = "sha256:" + "a" * 64
        if behavior == "withdraw":
            returned["configuration"]["active"] = False
        if behavior == "drift":
            domain["semantic_basis_id"] = "sha256:" + "f" * 64
        return returned
    session.execution.executor = SimpleNamespace(qualification_configuration=service)
    monkeypatch.setattr(driver, "gate", lambda *_args, **_kwargs: unlocked_gate(counter))
    monkeypatch.setattr(driver, "_begin", lambda *_args: {"state": {"current": "answering"}})
    monkeypatch.setattr(driver, "require_machine_compatible", lambda *_args: None)
    monkeypatch.setattr(probes, "domain_observation", lambda *_args: copy.deepcopy(domain))
    monkeypatch.setattr(driver, "deadline", lambda *_args: datetime.now(timezone.utc) + timedelta(seconds=60))
    monkeypatch.setattr(driver, "retained", lambda *_args: [])
    monkeypatch.setattr(driver, "execution_budget_overrun", lambda *_args: False)
    monkeypatch.setattr(driver, "append", lambda _session, kind, identifier, data: recorded.append((kind, identifier, copy.deepcopy(data))))
    return session, recorded, calls


@pytest.mark.parametrize("behavior", ["normal", "nonce", "basis", "drift", "withdraw"])
def test_external_probe_has_no_gate_and_refuses_replay_or_concurrent_context_drift(monkeypatch, candidate, behavior):
    session, recorded, calls = refresh_boundaries(monkeypatch, candidate, behavior=behavior)
    probes.refresh(session, "probe")
    assert len(calls) == len(recorded) == 1
    probe = recorded[0][2]["probe"]
    if behavior in {"nonce", "basis", "drift"}:
        assert probe["reasons"] and not probe["configurations"]
    else:
        assert probe["configurations"]["profile-a"]["active"] is (behavior != "withdraw")


def test_pure_qualification_gate_does_not_invoke_external_services(monkeypatch, candidate):
    monkeypatch.setattr(probes, "bounded_host_call", lambda *_args: pytest.fail("Pure registry called host service."))
    assert rows(candidate)[0]["qualified"] is True


@pytest.mark.parametrize("failure", ["intake_grant", "evaluate_grant", "deadline", "overrun"])
def test_explicit_current_probe_cannot_dispatch_without_original_authority_or_budget(monkeypatch, candidate, failure):
    session, recorded, calls = refresh_boundaries(monkeypatch, candidate)
    if failure.endswith("grant"):
        grant = "research_session." + ("intake_observation" if failure == "intake_grant" else "host_job.evaluate")
        def require(wanted):
            if wanted == grant:
                raise PermissionError("Missing explicit host grant.")
        session.context.require_grant = require
        expected = PermissionError
    else:
        expected = WorkflowContractError
        if failure == "deadline":
            monkeypatch.setattr(driver, "deadline", lambda *_: datetime.now(timezone.utc) - timedelta(seconds=1))
        else:
            monkeypatch.setattr(driver, "execution_budget_overrun", lambda *_: True)
    with pytest.raises(expected) as caught:
        probes.refresh(session, "bounded-current")
    if expected is WorkflowContractError:
        assert caught.value.error_code == "RESEARCH_BUDGET_EXHAUSTED"
    assert not calls and not recorded


def test_budget_loss_during_callback_retains_unknown_witness(monkeypatch, candidate):
    session, recorded, calls = refresh_boundaries(monkeypatch, candidate)
    monkeypatch.setattr(driver, "execution_budget_overrun", lambda *_: bool(calls))
    probes.refresh(session, "bounded-current")
    assert len(calls) == len(recorded) == 1
    probe = recorded[0][2]["probe"]
    assert not probe["configurations"] and probe["reasons"] == ["qualification_budget_exhausted"]


def test_current_probe_observes_fresh_nonce_each_time_without_extending_lifetime(monkeypatch, candidate):
    session, recorded, calls = refresh_boundaries(monkeypatch, candidate)
    probes.refresh(session, "first-current")
    probes.refresh(session, "second-current")
    assert len(calls) == len(recorded) == 2 and calls[0]["nonce"] != calls[1]["nonce"]
    for _, _, data in recorded:
        probe = data["probe"]
        assert (datetime.fromisoformat(probe["expires_utc"]) - datetime.fromisoformat(probe["observed_utc"])).total_seconds() == 30


def replay_boundaries(monkeypatch, *, terminal=True, state="complete"):
    from evidence_wiki.research_session_contracts import STEP_V2
    from tests.test_execution_permission_adversarial import accepted_observation, isolated_boundaries
    from tests.test_run_policy_scope import DIGEST, native_policy_document

    policy = RunPolicy(native_policy_document())
    isolated = isolated_boundaries(monkeypatch, policy)
    isolated.session.execution.profile.calibration = object()
    isolated.session.execution.profile.readability = None
    monkeypatch.setattr(driver, "machine_compatible", lambda *_: True)
    observed = accepted_observation(policy, 1)
    step = {"schema_version": STEP_V2, "request_id": "replay", "run_id": isolated.session.run_id,
        "binding_id": DIGEST, "before_basis_id": DIGEST, "outcome": "accepted" if terminal else "advanced",
        "reason": "current_outcomes" if terminal else "bounded_generate", "terminal": terminal,
        "decision": policy.decide(observed) if terminal else None, "job": None, "event_ref": None,
        "canonical_run_state": state, "accounting": copy.deepcopy(isolated.view["accounting"]),
        "historical": False, "action": "release" if terminal else "generate", "attempt_id": None,
        "authorized": False, "evidence_accepted": False}
    isolated.append(isolated.session, "execution_step", "retained", {"step": step, "decision_input": observed})
    owner = driver.owner
    monkeypatch.setattr(driver, "owner", lambda name: SimpleNamespace(TERMINAL_STATES={"complete", "no_ship"}) if name == "run_controller" else owner(name))
    monkeypatch.setattr(driver, "_document", lambda *_args: {"state": {"current": state}})
    monkeypatch.setattr(driver, "_finish", lambda *_args: {"state": {"current": state}})
    monkeypatch.setattr(driver, "_close", lambda *_args: None)
    monkeypatch.setattr(probes, "refresh", lambda *_args: pytest.fail("Historical replay dispatched a qualification probe."))
    return isolated, DIGEST, observed


@pytest.mark.parametrize("terminal,state", [(True, "complete"), (False, "answering")])
def test_exact_completed_or_nonterminal_step_replay_has_no_probe_or_journal_growth(monkeypatch, terminal, state):
    isolated, basis, _ = replay_boundaries(monkeypatch, terminal=terminal, state=state)
    before = copy.deepcopy(isolated.records)
    result = driver.advance(isolated.session, request_id="replay", expected_basis_id=basis)
    assert result["historical"]
    assert isolated.records == before


def test_conflicting_stored_step_is_refused_before_qualification_probe(monkeypatch):
    from evidence_wiki.errors import EvidenceWikiError

    isolated, _, _ = replay_boundaries(monkeypatch)
    before = copy.deepcopy(isolated.records)
    with pytest.raises(EvidenceWikiError):
        driver.advance(isolated.session, request_id="replay", expected_basis_id="sha256:" + "b" * 64)
    assert isolated.records == before


def test_unfinished_accepted_recovery_gets_current_withdrawal_before_finish(monkeypatch):
    isolated, basis, observed = replay_boundaries(monkeypatch, state="answering")
    fresh = copy.deepcopy(observed)
    for evaluator in fresh["evaluators"]:
        evaluator["qualified"] = None
    calls, finished = [], []
    monkeypatch.setattr(probes, "refresh", lambda *_args: calls.append("current-withdrawal"))
    monkeypatch.setattr(driver, "policy_observation", lambda *_args: copy.deepcopy(fresh))
    monkeypatch.setattr(driver, "_finish", lambda _session, decision: finished.append(copy.deepcopy(decision)) or {"state": {"current": "no_ship"}})
    result = driver.advance(isolated.session, request_id="replay", expected_basis_id=basis)
    assert calls == ["current-withdrawal"]
    assert result["decision"]["outcome"] != "accepted"
    assert finished[0]["outcome"] == result["decision"]["outcome"]
    assert any(record["data"].get("revalidates_request_id") == "replay" for record in isolated.records)


def test_scoped_manual_evaluation_replays_parent_payload_while_worker_payload_is_blinded(monkeypatch):
    from evidence_wiki.review_context import create_review_context
    from tests.test_execution_permission_adversarial import isolated_boundaries
    from tests.test_review_context import inputs as review_inputs
    from tests.test_run_policy_scope import native_policy_document

    isolated = isolated_boundaries(monkeypatch, RunPolicy(native_policy_document()))
    isolated.session.execution.profile.calibration = object()
    supplied = review_inputs()
    context = create_review_context(**supplied)
    caller_payload = {"claim_id": supplied["owner_snapshot"]["claim"]["id"], "packet_id": context.binding()["owner_snapshot_sha256"]}
    worker_job = {"id": "manual-review", "schema_version": JOB_V3, "operation": "evaluate", "original_text": "original",
        "payload": {"claim_id": "claim-1", "packet_id": context.packet_id}}
    isolated.append(isolated.session, "execution_attempt", "stored-review", {"attempt": {"job": worker_job},
        "claim_id": caller_payload["claim_id"], "review_context": context.binding()})
    monkeypatch.setattr(probes, "refresh", lambda *_args: pytest.fail("Stored manual evaluation replay invoked external probe."))
    before = copy.deepcopy(isolated.records)
    result = driver.host_job(isolated.session, request_id="manual-review", operation="evaluate", payload=caller_payload)
    assert result == worker_job
    assert isolated.records == before


@pytest.mark.parametrize("field", ["api_key", "credentials", "signing_key"])
def test_secret_configuration_fields_are_refused_before_durable_journal(monkeypatch, candidate, field):
    session, recorded, calls = refresh_boundaries(monkeypatch, candidate)
    original = session.execution.executor.qualification_configuration
    secret = "private-host-material-must-not-be-persisted"
    def unsafe_configuration(request):
        response = original(request)
        response["configuration"][field] = {"value": secret} if field == "credentials" else secret
        return response
    session.execution.executor.qualification_configuration = unsafe_configuration
    probes.refresh(session, "unsafe-host-configuration")
    assert len(calls) == len(recorded) == 1
    probe = recorded[0][2]["probe"]
    assert probe["reasons"] and probe["configurations"] == {}
    assert secret not in str(recorded)
    assert field not in str(recorded)


@pytest.mark.parametrize("field,value", [("active", "true"), ("active", 1), ("execution_kind", ["model"])])
def test_host_configuration_flags_are_typed_before_journal_intake(monkeypatch, candidate, field, value):
    session, recorded, _ = refresh_boundaries(monkeypatch, candidate)
    original = session.execution.executor.qualification_configuration
    def malformed(request):
        response = original(request)
        response["configuration"][field] = value
        return response
    session.execution.executor.qualification_configuration = malformed
    probes.refresh(session, "malformed-host-configuration")
    assert recorded[0][2]["probe"]["reasons"]
    assert recorded[0][2]["probe"]["configurations"] == {}
