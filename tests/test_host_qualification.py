"""Bounded observation behavior; synthetic records are not native qualification."""

import json
from types import SimpleNamespace

import pytest

from evidence_wiki import host_qualification
from evidence_wiki.errors import ConfigError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_session import ResearchSession


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    session = object.__new__(ResearchSession)
    session._run_id = "scoped-run"
    session._root = tmp_path
    session._binding_raw = json.dumps({"scope": "synthetic-only", "owner_generation": {
        "package_sha256": "0" * 64, "script_sha256": "sha256:" + "0" * 64}}).encode()
    session._context = SimpleNamespace(require_grant=lambda name: None)
    session._policy = SimpleNamespace(policy_id=digest("frozen-policy"), to_dict=lambda: {"assurance": "artifact_checked", "required_evaluators": []})
    records = []
    session._execution = SimpleNamespace(check=lambda binding: None, records=lambda session: records,
        profile=SimpleNamespace(sha256="frozen-profile"), qualifications=SimpleNamespace(sha256="frozen-registry"), executor=object())
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
    assert result["attempts"] == []
    assert result["control"]["distinct_native_review_controllers"] is None
    assert result["control"]["independent_credential_custody"] == "not_observed"
    assert result["capability_matrix"][0]["semantic_eligibility"] == "unknown"
    assert result["capability_matrix"][1]["state"] == "unsupported"
    assert result["capability_matrix"][2]["state"] == "not_observed"
    assert not result["authorized"] and not result["evidence_accepted"]


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


def test_explicit_owning_grant_is_required(synthetic):
    seen = []
    def denied(name):
        seen.append(name)
        raise ConfigError("DENIED", "Missing owning grant")
    synthetic[0].context.require_grant = denied
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


@pytest.mark.parametrize("indexes", [(0, 0, 0), (2, 1, 0), (0, 2, 1)])
def test_report_rejects_duplicate_or_reordered_boundary_cells(synthetic, indexes):
    from evidence_wiki.host_qualification_contracts import HOST, validate

    result = host_qualification(synthetic[0])
    result["capability_matrix"] = [result["capability_matrix"][index] for index in indexes]
    with pytest.raises(ConfigError) as caught:
        validate(HOST, result)
    assert caught.value.details["reason"] == "capability_matrix_selection_invalid"


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
