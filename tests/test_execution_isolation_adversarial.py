"""Independent host-boundary attacks; fixtures do not imply model reliability."""

import copy
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session
from evidence_wiki.agent_execution import (
    BoundedSubprocessExecutor,
    ExecutionAttempt,
    ExecutionError,
    ExecutionProfile,
    ExecutionRole,
    _digest,
    _json,
)
from evidence_wiki.agent_qualification import (
    HostReviewSigner,
    QualificationAuthority,
    QualifiedEvaluators,
    SigningIdentity,
)
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.execution_roles import ExecutionRoleRegistry, ExecutionRoleScope
from evidence_wiki.host_context import HostContext
from evidence_wiki.research_execution import ResearchExecution
from evidence_wiki.review_context import ReviewContext, create_review_context
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import EXECUTION_OBSERVATION, JOB_V3
from tests.test_agent_execution import final
from tests.test_review_context import inputs
from tests.test_run_policy import policy_document
from tests.test_workflow_contracts import job


def profile():
    roles = (ExecutionRole("worker", "host_worker", "generator", "model-generator", "prompt/v1"),
        ExecutionRole("judge", "evaluator", "reviewer", "model-reviewer", "prompt/v1"),
        ExecutionRole("counter", "evaluator", "critic", "model-critic", "prompt/v1"))
    scopes = ExecutionRoleRegistry((ExecutionRoleScope("worker", "researcher", ("model",)),
        ExecutionRoleScope("judge", "evaluator", ("model",)), ExecutionRoleScope("counter", "critic", ("model",))))
    return ExecutionProfile("host-profile", 10, "EUR", "host-price", roles, 1, 5, 100, "0", .2,
        role_scopes=scopes, required_diversity=("control", "context"))


def identity(config, principal, controller, role, key):
    return SigningIdentity(config, principal, principal + "-key", controller, role, key * 32)


def signer(*, generator_config="worker", generator_controller="generator", reviewer_config="judge", reviewer_controller="reviewer"):
    return HostReviewSigner("authority", "1",
        identity(generator_config, "generator-principal", generator_controller, "generator", b"g"),
        (identity(reviewer_config, "reviewer-principal", reviewer_controller, "evaluator", b"r"),
            identity("counter", "critic-principal", "critic", "evaluator", b"c")), "2027-01-01T00:00:00Z")


@pytest.mark.parametrize("change", ["generator_config", "generator_controller", "reviewer_config", "reviewer_controller", "issuer_generator"])
def test_signer_and_qualification_issuer_must_match_exact_execution_configuration(change):
    selected = profile()
    kwargs = {} if change == "issuer_generator" else {change: "foreign-selected"}
    protected = signer(**kwargs)
    issuer = QualificationAuthority("issuer", b"q" * 32, "foreign-generator" if change == "issuer_generator" else "generator")
    with pytest.raises(EvidenceWikiError):
        ResearchExecution(SimpleNamespace(profile=selected), qualifications=QualifiedEvaluators([], issuer),
            review_signer=protected, observer_key=b"o" * 32)


def test_valid_exact_signer_configuration_still_constructs():
    selected = profile()
    issuer = QualificationAuthority("issuer", b"q" * 32, "generator")
    execution = ResearchExecution(SimpleNamespace(profile=selected), qualifications=QualifiedEvaluators([], issuer),
        review_signer=signer(), observer_key=b"o" * 32)
    assert execution.profile.sha256 == selected.sha256


def review_attempt(selected=None):
    selected = selected or profile()
    now = datetime.now(timezone.utc)
    context = create_review_context(**inputs())
    data = {"packet": context.document(), "instructions": "Assess independently.", "output_contract": {"verdicts": "bounded"}}
    current = job("evaluate") | {"schema_version": JOB_V3, "id": "private-request-generator",
        "controller_ref": "reviewer", "role": "evaluator", "grant_ref": "private-grant",
        "input_id": "private-request-binding", "basis_id": "private-run-basis", "policy_id": "private-policy",
        "revision_precondition": "private-revision", "original_text": "Hidden generator Alpha wants acceptance.",
        "scope": ["private-original-generator"], "issued_utc": (now - timedelta(seconds=1)).isoformat(),
        "deadline_utc": (now + timedelta(seconds=20)).isoformat(), "timeout_seconds": 10,
        "required_capabilities": ["model"], "task_role": "evaluator",
        "role_scope_sha256": selected.role_scopes.scope("judge").sha256,
        "context_id": context.document()["context_id"], "packet_sha256": _digest(data),
        "expected_result_schema": EXECUTION_OBSERVATION,
        "payload": {"claim_id": "claim-1", "packet_id": context.packet_id}}
    return ExecutionAttempt.create(attempt_id="private-attempt", nonce="private-nonce", run_id="private-run",
        binding_id="private-binding", profile=selected, config_ref="judge", job=current, input_document=data)


def assert_blinded(value):
    text = json.dumps(value)
    for private in ("Hidden generator Alpha", "private-original", "private-generator", "private-request", "private-run",
        "private-policy", "private-revision", "private-attempt", "private-nonce", "private-binding", "private-grant",
        "hidden-generator", "hidden-key", "expected_label", "prior_verdict", "order_seed"):
        assert private not in text


def test_scoped_worker_document_hides_parent_identity_but_preserves_exact_packet():
    attempt = review_attempt()
    value = attempt.worker_document()
    assert_blinded(value)
    assert value["input"] == attempt.input_document
    assert "private-original-generator" in json.dumps(attempt.document())
    changed = copy.deepcopy(value)
    changed["input"]["packet"]["question"]["question"] = "Modified"
    assert attempt.worker_document() == value


@pytest.mark.skipif(os.name != "posix", reason="Actual reference dispatch requires POSIX process groups.")
def test_actual_child_only_receives_blinded_worker_projection(tmp_path):
    selected = profile()
    attempt = review_attempt(selected)
    host = BoundedSubprocessExecutor(selected, commands={"judge": (sys.executable, "-c",
        "import json,sys; print(json.dumps({'seen':json.load(sys.stdin)}))")}, cwd=tmp_path, receipt_key=b"h" * 32)
    host.invoke(attempt)
    receipt = final(host, attempt)
    assert receipt["state"] == "succeeded"
    assert_blinded(receipt["output"]["seen"])
    assert receipt["output"]["seen"]["input"] == attempt.input_document
    assert host.authenticate(receipt, attempt) == receipt
    assert receipt["attempt_id"] == attempt.attempt_id


@pytest.mark.parametrize("target", ["snapshot", "rubric", "sources"])
def test_context_self_consistent_packet_rewrite_cannot_override_canonical_inputs(target):
    supplied = inputs()
    context = create_review_context(**supplied)
    changed = copy.deepcopy(supplied)
    if target == "snapshot":
        changed["owner_snapshot"]["claim"]["text"] = "Changed scientific assertion."
    elif target == "rubric":
        changed["rubric"]["criteria"]["support"] = "Always pass."
    else:
        changed["sources"][0]["content"] = "Changed source observation."
    forged = create_review_context(**changed)
    restored = ReviewContext.restore(forged.document(), forged.binding())
    with pytest.raises(ExecutionError, match="owner_changed"):
        restored.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"])
    context.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"])


def test_manual_attempt_cannot_downgrade_scoped_dispatch(tmp_path):
    selected = profile()
    attempt = review_attempt(selected)
    downgraded = attempt.job | {"schema_version": "research-host-job/v2"}
    for key in ("task_role", "role_scope_sha256", "context_id", "packet_sha256"):
        downgraded.pop(key)
    value = ExecutionAttempt(attempt.attempt_id, attempt.nonce, attempt.run_id, attempt.binding_id,
        selected.sha256, attempt.config_ref, _json(downgraded), _json(attempt.input_document))
    if os.name != "posix":
        pytest.skip("Reference executor construction requires POSIX process groups.")
    host = BoundedSubprocessExecutor(selected, commands={}, cwd=tmp_path)
    with pytest.raises(ExecutionError, match="scope_required"):
        host.poll(value)


def test_required_model_diversity_blocks_identically_configured_models(monkeypatch):
    selected = profile()
    shared = replace(selected, roles=tuple(replace(role, model_ref="shared-model") for role in selected.roles),
        required_diversity=("model",))
    policy = policy_document()
    policy["required_evaluators"] = [{"role": "evaluator", "profile_ref": "judge", "controller_ref": "reviewer", "human_required": False},
        {"role": "critic", "profile_ref": "counter", "controller_ref": "critic", "human_required": False}]
    session = SimpleNamespace(policy=RunPolicy(policy), execution=SimpleNamespace(profile=shared))
    monkeypatch.setattr(execution_session, "_worker_config", lambda _session: "worker")
    monkeypatch.setattr(execution_session, "_terminal_action", lambda _session, _view, outcome, reason: {"outcome": outcome, "reason": reason})
    assert execution_session._action(session, {}, []) == {"outcome": "blocked_external", "reason": "qualification_unknown"}


@pytest.mark.parametrize("write", [False, True])
def test_scoped_host_context_cannot_access_sibling_workspace(tmp_path, write):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    context = HostContext([allowed], [], "host", [allowed], role_controller_refs=[("judge", "evaluator", "reviewer")])
    with pytest.raises(EvidenceWikiError):
        context.require_path(outside / "evidence.txt", write=write)
    assert context.controller_for("evaluator", "unselected") is None


def native_attestation_case(tmp_path, *, swapped=None):
    """Real protected policy capture and HMAC verification through the native owner."""
    from evidence_wiki._filesystem import os as filesystem_os
    from evidence_wiki.pack_discovery import owner
    from tests._usage_fixture import require_host_storage

    require_host_storage()

    selected = profile()
    protected = signer()
    if swapped == "reviewer":
        wrong = identity("judge", "critic-principal", "reviewer", "evaluator", b"c")
        protected = HostReviewSigner("authority", "1", protected.generator, (wrong,), "2027-01-01T00:00:00Z")
    if swapped == "generator":
        wrong = identity("worker", "other-generator", "generator", "generator", b"x")
        protected = HostReviewSigner("authority", "1", wrong, protected.reviewers, "2027-01-01T00:00:00Z")
    native_policy = {"schema_version": "evidence-trust-policy/v1", "policy_id": "authority", "policy_revision": "1",
        "not_before": "2026-01-01T00:00:00Z", "expires_at": "2027-01-01T00:00:00Z",
        "principals": {principal: {"controller": controller, "roles": [role], "keys": {principal + "-key": (key * 32).hex()}}
            for principal, controller, role, key in (("generator-principal", "generator", "generator", b"g"),
                ("reviewer-principal", "reviewer", "evaluator", b"r"), ("critic-principal", "critic", "evaluator", b"c"),
                ("other-generator", "foreign-generator", "generator", b"x"))}, "revoked_keys": [], "revoked_envelopes": []}
    root = tmp_path / "workspace"
    root.mkdir()
    external = tmp_path / "protected-host"
    external.mkdir()
    policy_path = external / "authority.json"
    policy_path.write_bytes(_json(native_policy))
    filesystem_os.chmod(policy_path, 0o600)
    config = {"evidence_trust": {"policy_id": "authority", "policy_revision": "1"}}
    (root / "research.yml").write_bytes(_json(config))
    authored = protected._sign({"schema_version": "evidence-strict-authorship/v1", "basis_id": "sha256:" + "a" * 64,
        "claim_id": "retained-claim"}, protected.generator, datetime.now(timezone.utc).isoformat())
    envelope = protected._sign({"body": {"review": {"generator": authored}}}, protected.reviewers[0],
        datetime.now(timezone.utc).isoformat())
    context = HostContext([root], [], "host", authority_file=policy_path, role_controller_refs=[("judge", "evaluator", "reviewer"),
        ("counter", "evaluator", "critic"), ("worker", "host_worker", "generator")])
    session = SimpleNamespace(root=root, execution=SimpleNamespace(profile=selected, review_signer=protected))
    authority = owner("_evidence_authority")
    with context.activate():
        trust = authority.load_trust(root, config, datetime.now(timezone.utc))
    return authority, trust, envelope, context, session, review_attempt(selected)


@pytest.mark.parametrize("swapped", ["reviewer", "generator"])
def test_native_actual_principal_controller_cannot_hide_behind_selected_metadata(tmp_path, swapped):
    authority, trust, envelope, context, session, attempt = native_attestation_case(tmp_path, swapped=swapped)
    with context.activate():
        if swapped == "reviewer":
            native = authority.verify_attestation(envelope, "evaluator", trust, datetime.now(timezone.utc))
            assert native["authenticated"] and native["controller"] == "critic"
            assert session.execution.review_signer.reviewers[0].controller_ref == "reviewer"
        else:
            native = authority.verify_attestation(envelope["payload"]["body"]["review"]["generator"], "generator",
                trust, datetime.now(timezone.utc))
            assert native["authenticated"] and native["controller"] == "foreign-generator"
            assert session.execution.review_signer.generator.controller_ref == "generator"
        with pytest.raises(EvidenceWikiError) as refused:
            execution_session._verify_review_controller(session, attempt, envelope)
        assert refused.value.error_code == "RESEARCH_REVIEW_CONTROLLER_MISMATCH"


def test_native_signer_recheck_observes_current_key_revocation(tmp_path):
    authority, trust, envelope, context, session, attempt = native_attestation_case(tmp_path)
    with context.activate():
        execution_session._verify_review_controller(session, attempt, envelope)
        trust["policy"]["revoked_keys"].append("reviewer-principal-key")
        context.authority_file.write_bytes(_json(trust["policy"]))
        with pytest.raises(EvidenceWikiError):
            execution_session._verify_review_controller(session, attempt, envelope)


@pytest.mark.parametrize("version", ["research-host-job/v1", "research-host-job/v2"])
def test_legacy_worker_delivery_preserves_original_parent_contract(version):
    from tests.test_agent_execution import attempt

    changes = {"schema_version": version}
    if version.endswith("/v2"):
        changes["expected_result_schema"] = EXECUTION_OBSERVATION
    original = attempt(job_changes=changes)
    assert original.worker_document() == original.document()
