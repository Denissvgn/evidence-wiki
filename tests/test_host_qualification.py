"""Bounded observation behavior; synthetic records are not native qualification."""

import json
from types import SimpleNamespace

import pytest

from evidence_wiki import host_qualification
from evidence_wiki.errors import ConfigError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_session import ResearchSession


@pytest.fixture
def synthetic(monkeypatch, tmp_path, request):
    session = object.__new__(ResearchSession)
    session._run_id = "scoped-run"
    session._root = tmp_path
    session._binding_raw = json.dumps({"scope": "synthetic-only", "owner_generation": {
        "package_sha256": "0" * 64, "script_sha256": "sha256:" + "0" * 64}}).encode()
    from evidence_wiki.host_context import HostContext
    from evidence_wiki.research_execution import ResearchExecution

    (tmp_path / "research.yml").write_text("{}\n")
    session._closed, session._owner_workspace = False, None
    session._context = HostContext(allowed_roots=(tmp_path,), grants=frozenset({'host_qualification.current'}),
        actor_ref='synthetic-host')
    active = session.context.activate()
    active.__enter__()
    request.addfinalizer(lambda: active.__exit__(None, None, None))
    session._policy = SimpleNamespace(policy_id=digest("frozen-policy"), to_dict=lambda: {"assurance": "artifact_checked", "required_evaluators": []})
    records = []
    execution = object.__new__(ResearchExecution)
    execution._profile = SimpleNamespace(sha256="frozen-profile", review_delivery=None,
        document=lambda: {"profile": "synthetic"})
    execution._qualifications = SimpleNamespace(sha256="frozen-registry")
    execution._review_signer = SimpleNamespace(sha256="frozen-signer")
    execution._observer_key, execution._executor = b"s" * 32, object()
    execution.check, execution.records = lambda binding: None, lambda session: records
    session._execution = execution
    monkeypatch.setattr(ResearchSession, "_check", lambda self, operation: None)
    monkeypatch.setattr(ResearchSession, "current", lambda self: {"basis_id": digest("current-basis"), "qualification": {"state": "unknown"}})
    monkeypatch.setattr("evidence_wiki.execution_session.owner", lambda name: SimpleNamespace(configuration=lambda root: {}))
    monkeypatch.setattr("evidence_wiki.service_authentication.status", lambda selected: {
        "schema_version": "research-review-service-status/v1", "run_id": session.run_id,
        "binding_id": digest(session.binding), "profile_id": "sha256:" + "0" * 64,
        "policy_id": session.policy.policy_id, "authority_id": None, "observed_at": "2026-10-01T12:00:00Z",
        "state": "blocked", "reason": "synthetic_missing_credential", "services": [{
            "config_ref": "generator", "slot_ref": None, "task_role": "researcher", "principal": None,
            "controller_ref": "synthetic", "credential_state": "unavailable", "qualification_state": "not_required",
            "execution_state": "not_observed", "required": True, "reason": "synthetic_missing_credential",
            "challenge_ref": None, "owned_observation_ref": None}],
        "credential_probe": "parent_native_credential_challenge", "execution_probe": "existing_correlated_owned_attempts_only",
        "enrollment": "not_performed", "authorized": False, "evidence_accepted": False})
    return session, records


def test_missing_execution_and_caller_mapping_do_not_create_host_evidence():
    with pytest.raises(ConfigError, match="Host qualification"):
        host_qualification({"controllers": ["one", "two"], "assurance": "host_enforced"})


def test_empty_owner_inventory_never_invents_native_attestation_or_kernel_isolation(synthetic):
    result = host_qualification(synthetic[0])
    assert result["schema_version"] == "research-host-qualification/v1"
    assert result["attempts"] == []
    assert result["control"]["distinct_native_review_controllers"] is None
    assert result["control"]["independent_credential_custody"] == "not_observed"
    assert result["capability_matrix"][0]["semantic_eligibility"] == "unknown"
    assert result["capability_matrix"][1]["state"] == "unsupported"
    assert result["capability_matrix"][2]["state"] == "not_observed"
    assert not result["authorized"] and not result["evidence_accepted"]


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("authentication_refused", [False, True])
def test_authenticated_selective_application_selects_closed_report_version(synthetic, monkeypatch, selected, authentication_refused):
    from contextlib import nullcontext
    from datetime import datetime, timedelta, timezone

    from evidence_wiki import execution_session as driver
    from evidence_wiki.errors import UsageError
    from evidence_wiki.host_qualification_contracts import HOST, HOST_V2, validate

    session, records = synthetic
    monkeypatch.setattr("evidence_wiki._host_qualification.digest_record", digest)
    now = datetime.now(timezone.utc)
    identity = SimpleNamespace(config_ref="reviewer", controller_ref="review-controller",
        principal="review-principal", key_id="review-key")
    generator = SimpleNamespace(controller_ref="generator-controller", principal="generator", key_id="generator-key")
    attempt = SimpleNamespace(attempt_id="retained-attempt", config_ref="reviewer",
        job={"deadline_utc": (now + timedelta(hours=1)).isoformat()}, input_document={"packet": {}},
        document=lambda: {"attempt": "synthetic"})
    role = SimpleNamespace(role="evaluator", controller_ref=identity.controller_ref, model_ref="synthetic-model")
    session.execution._profile.role = lambda config: role
    session.execution._review_signer = SimpleNamespace(sha256="frozen-signer", reviewers=[identity], generator=generator)
    session.execution.attempt = lambda retained: attempt
    review = {"schema_version": "evidence-strict-review/v3", "basis_id": digest("basis"), "claim_id": "claim",
        "claim_schema_version": "evidence-strict-claims/v2", "observed_workspace_basis_id": digest("workspace"),
        "delivery": {"semantic_material_id": digest("material")}, "observation": {"state": "synthetic"},
        "reviewed_at": now.isoformat()}
    review["generator"] = {"authentication": {"principal": generator.principal, "key_id": generator.key_id},
        "payload": {"schema_version": "evidence-strict-authorship/v2", "basis_id": review["basis_id"],
            "claim_id": "claim", "claim_schema_version": review["claim_schema_version"],
            "observed_workspace_basis_id": review["observed_workspace_basis_id"],
            "semantic_material_id": review["delivery"]["semantic_material_id"]}}
    envelope = {"payload": {"body": {"review": review}}, "authentication": {
        "principal": identity.principal, "key_id": identity.key_id, "issued_at": now.isoformat()}}
    records.extend([
        {"kind": "execution_attempt", "data": {"attempt": {}, "claim_id": "claim", "review_context": {}}},
        {"kind": "execution_observation", "data": {"attempt_id": attempt.attempt_id}},
        {"kind": "execution_application", "data": {"attempt_id": attempt.attempt_id, "profile_ref": "reviewer",
            "claim_id": "claim", "strict_basis_id": review["basis_id"], "envelope": envelope}}])
    prepared = {"result": {"claims": [{"claim": {"id": "claim"}, "review": {"review_id": "selected" if selected else "other"},
        "claim_basis_id": review["basis_id"], "observation": review["observation"]}]},
        "snapshot": {"policy": {"max_review_age_seconds": 60}}}
    owner = driver.owner
    monkeypatch.setattr(driver, "owner", lambda name: SimpleNamespace(artifact_id=lambda value: "selected")
        if name == "_strict_contract" else owner(name))
    monkeypatch.setattr(driver, "_host_sources", lambda session: ({}, None))
    monkeypatch.setattr(driver, "_check_review_context", lambda *args: None)
    verified = []
    def verify(*args):
        verified.append(args)
        if authentication_refused:
            raise ConfigError("SYNTHETIC_AUTHENTICATION_REFUSED", "Synthetic controller authentication refused.")
    monkeypatch.setattr(driver, "_verify_review_controller", verify)
    monkeypatch.setattr("evidence_wiki.service_authentication.owned", lambda *args: ({}, attempt,
        {"state": "succeeded", "final_accounting": True, "completed_utc": now.isoformat()}))
    monkeypatch.setattr("evidence_wiki.review_context.ReviewContext.restore", lambda *args: SimpleNamespace(
        document=lambda: {"manifest": {"context_complete": True}}, binding=lambda: {"context": "synthetic"}))
    monkeypatch.setattr("evidence_wiki.workspace.Workspace.open", lambda root: nullcontext(SimpleNamespace(
        strict=SimpleNamespace(prepare_review=lambda claim: prepared))))
    proof_calls = []
    monkeypatch.setattr("evidence_wiki._review_dependency_custody.resolve_original", lambda *args, **kwargs:
        SimpleNamespace(assert_review_envelope=lambda value: proof_calls.append(value)))
    if authentication_refused:
        with pytest.raises(ConfigError, match="Synthetic controller authentication refused"):
            host_qualification(session)
        assert len(verified) == 1 and proof_calls == []
        return
    witness = host_qualification(session)
    assert witness["schema_version"] == HOST_V2
    assert len(witness["attempts"]) == 1 and len(verified) == 1
    assert proof_calls == ([envelope] if selected else [])
    row = witness["attempts"][0]
    assert row["context_state"] == ("current_selective_semantics" if selected else "current_immutable_packet")
    assert row["attestation_state"] == ("current_bound_review" if selected else "native_controller_verified")
    assert validate(HOST_V2, witness) == witness
    if selected:
        legacy = {**witness, "schema_version": HOST}
        with pytest.raises(UsageError):
            validate(HOST, legacy)


def test_host_policy_is_independently_unsupported_without_semantic_boundary(synthetic):
    synthetic[0].policy.to_dict = lambda: {"assurance": "host_enforced", "required_evaluators": []}
    result = host_qualification(synthetic[0])
    assert result["capability_matrix"][0]["state"] == "unsupported"
    assert result["capability_matrix"][1]["state"] == "unsupported"


def test_closed_or_ungranted_scope_is_checked_before_any_credential_probe(synthetic, monkeypatch):
    def refuse(*args):
        raise ConfigError("CLOSED", "Closed scoped owner")
    monkeypatch.setattr(ResearchSession, "_check", refuse)
    monkeypatch.setattr("evidence_wiki.service_authentication.status", lambda selected: pytest.fail("Closed owner probed credentials"))
    with pytest.raises(ConfigError, match="Closed scoped owner"):
        host_qualification(synthetic[0])


def test_explicit_owning_grant_is_required(synthetic, monkeypatch):
    seen = []
    def denied(_context, name):
        seen.append(name)
        raise ConfigError("DENIED", "Missing owning grant")
    monkeypatch.setattr(type(synthetic[0].context), 'require_grant', denied)
    with pytest.raises(ConfigError, match="Missing owning grant"):
        host_qualification(synthetic[0])
    assert seen == ["host_qualification.current"]


def test_inventory_bound_precedes_native_receipt_loading(synthetic, monkeypatch):
    synthetic[1].extend({"kind": "execution_attempt", "data": {}} for _ in range(65))
    monkeypatch.setattr("evidence_wiki.service_authentication.status", lambda selected: pytest.fail("Unbounded inventory probed"))
    with pytest.raises(ConfigError) as caught:
        host_qualification(synthetic[0])
    assert caught.value.details["reason"] == "attempt_inventory_bound_exceeded"


def test_closing_scope_change_invalidates_the_dated_observation(synthetic, monkeypatch):
    calls = iter(("before", "after"))
    monkeypatch.setattr(ResearchSession, "current", lambda self: {"basis_id": next(calls), "qualification": {"state": "unknown"}})
    with pytest.raises(ConfigError) as caught:
        host_qualification(synthetic[0])
    assert caught.value.details["reason"] == "observation_changed"


def test_private_module_import_cannot_shadow_the_public_callable():
    import evidence_wiki
    import evidence_wiki._host_qualification
    assert callable(evidence_wiki.host_qualification)


def test_host_report_versions_are_discoverable_with_unchanged_legacy_shape():
    from copy import deepcopy

    from evidence_wiki import contract
    from evidence_wiki.agent_resources import resource_document
    from evidence_wiki.host_qualification_contracts import HOST, HOST_V2, schemas

    descriptor = contract()["host_qualification"]
    assert descriptor["schema"] == HOST and descriptor["selective_schema"] == HOST_V2
    assert descriptor["limits"] == {"attempts": 64, "report_bytes": 262144}
    documents = schemas()
    for identifier in (HOST, HOST_V2):
        assert descriptor["schema_documents"][identifier] == documents[identifier]
        assert json.loads(resource_document(identifier)["content"]) == documents[identifier]
    expected = deepcopy(documents[HOST])
    expected["properties"]["schema_version"] = {"const": HOST_V2}
    expected["properties"]["attempts"]["items"]["properties"]["context_state"]["enum"].append("current_selective_semantics")
    assert documents[HOST_V2] == expected


@pytest.mark.parametrize("version", ["research-host-qualification/v1", "research-host-qualification/v2"])
def test_report_version_does_not_renew_observer_window(synthetic, version):
    from datetime import datetime, timedelta

    from tools._observation_freshness import observation_policy, observer_deadline, validate_observer_window
    from tools._review_dependency_workload import digest as workload_digest

    witness = host_qualification(synthetic[0])
    witness["schema_version"] = version
    policy = observation_policy("batch")
    observed = {"native_host_witnesses": [witness], "observation_policy": policy,
        "observation_policy_sha256": workload_digest(policy), "witness_semantics": policy["witness_semantics"],
        "observer_recency_not_after": observer_deadline([witness], policy)}
    closing = datetime.fromisoformat(witness["observed_utc"]) + timedelta(seconds=1)
    assert validate_observer_window(observed, closing, policy) == observed["observer_recency_not_after"]
    with pytest.raises(ValueError, match="workload_original_witness_expired"):
        validate_observer_window(observed, datetime.fromisoformat(observed["observer_recency_not_after"]), policy)


@pytest.mark.parametrize("version", ["research-host-qualification/v1", "research-host-qualification/v2"])
@pytest.mark.parametrize("indexes", [(0, 0, 0), (2, 1, 0), (0, 2, 1)])
def test_report_rejects_duplicate_or_reordered_boundary_cells(synthetic, indexes, version):
    from evidence_wiki.host_qualification_contracts import validate

    result = host_qualification(synthetic[0])
    result["schema_version"] = version
    result["capability_matrix"] = [result["capability_matrix"][index] for index in indexes]
    with pytest.raises(ConfigError) as caught:
        validate(version, result)
    assert caught.value.details["reason"] == "capability_matrix_selection_invalid"


@pytest.mark.parametrize("version", ["research-host-qualification/v1", "research-host-qualification/v2"])
def test_complete_attempt_inventory_preserves_report_byte_bound(synthetic, version):
    from evidence_wiki.host_qualification_contracts import validate

    result = host_qualification(synthetic[0])
    result["schema_version"] = version
    hashed = digest("synthetic")
    row = {"attempt_id": "retained", "config_ref": "reviewer", "attempt_ref": hashed,
        "controller_ref": "controller", "model_ref": "model", "execution_state": "succeeded",
        "observation_ref": hashed, "context_state": "not_current", "context_ref": hashed,
        "attestation_state": "native_controller_verified", "attestation_ref": hashed,
        "attestation_principal": "principal", "attestation_key_id": "key", "application_state": "registered",
        "context_reason": "historical", "lease_state": "expired"}
    result["attempts"] = [{**row, "attempt_id": str(index)} for index in range(64)]
    assert validate(version, result)["attempts"] == result["attempts"]
    for attempt in result["attempts"]:
        for key in ("config_ref", "controller_ref", "model_ref", "attestation_principal", "attestation_key_id", "context_reason"):
            attempt[key] = "界" * 256
    with pytest.raises(ConfigError) as caught:
        validate(version, result)
    assert caught.value.details["reason"] == "report_bound_exceeded"


def test_signed_historical_review_survives_unrelated_global_basis_change_without_current_credit(tmp_path, monkeypatch):
    import copy
    from dataclasses import replace

    from evidence_wiki import Workspace
    from evidence_wiki import execution_session as native
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.review_context import ReviewContext
    from evidence_wiki.review_packet_execution import reserve_evaluator
    from evidence_wiki.review_page_session import assembled
    from tests.test_review_packet_execution import configured, finish

    root, policy, execution, context, claim, _captured = configured.__wrapped__(tmp_path, monkeypatch)
    context = replace(context, grants=context.grants | {"host_qualification.current", "service_authentication.status"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="retained-review", execution=execution) as session:
            for config, task_role in (("profile-a", "evaluator"), ("profile-b", "critic")):
                assembly_id = config + "-pages"
                assembled(session, request_id=assembly_id, claim_id=claim["id"], task_role=task_role, page_chars=1024)
                actual = reserve_evaluator(session, request_id=config + "-review", assembly_request_id=assembly_id,
                    original_id="q", claim_id=claim["id"], config_ref=config)
                completed = finish(session, actual, config)
                native.intake(session, completed, request_id=config + "-final")
                native._apply(session, actual.attempt_id)
            records = execution.records(session)
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt" and "review_context" in row["data"]]
            assert attempts
            retained = attempts[0]
            attempt = execution.attempt(retained["attempt"])
            packet = ReviewContext.restore(attempt.input_document["packet"], retained["review_context"])
            before = workspace.strict.prepare_review(retained["claim_id"])
            packet.check_owner(owner_snapshot=before["snapshot"], basis_id=before["result"]["basis_id"])
            claims_path = root / "docs/research-claims.json"
            inventory = json.loads(claims_path.read_bytes())
            unrelated = copy.deepcopy(inventory["claims"][0])
            unrelated.update(id="independent-observation", text="The retained independent observation states the same measured reflectance.")
            inventory["claims"].append(unrelated)
            claims_path.write_text(json.dumps(inventory))
            after = workspace.strict.prepare_review(retained["claim_id"])
            assert before["snapshot"] == after["snapshot"]
            assert before["result"]["basis_id"] != after["result"]["basis_id"]
            with native._current_facts(session, operation="current"):
                witness = host_qualification(session)
            historical = [row for row in witness["attempts"] if row["application_state"] == "registered"]
            assert historical
            assert all(row["context_state"] == "not_current" and row["attestation_state"] == "native_controller_verified" for row in historical)
            assert witness["control"]["authenticated_review_controller_refs"] == []
            assert witness["capability_matrix"][0]["state"] == "not_observed"
            assert not witness["authorized"] and not witness["evidence_accepted"]
            application = next(row["data"] for row in records if row["kind"] == "execution_application"
                and row["data"].get("attempt_id") == attempt.attempt_id and "envelope" in row["data"])
            forged = copy.deepcopy(application["envelope"])
            forged["payload"]["body"]["review"]["basis_id"] = after["result"]["basis_id"]
            with pytest.raises(EvidenceWikiError):
                native._verify_review_controller(session, attempt, forged)
