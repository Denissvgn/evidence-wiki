"""Reference subprocess delivery tests establish protocol, never live model meaning."""

import ast
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from evidence_wiki import ExecutionAttempt, HostReviewSigner, Workspace
from evidence_wiki import execution_session as native
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.pack_discovery import owner
from evidence_wiki.review_packet_execution import reserve_evaluator
from evidence_wiki.review_page_session import assembled
from tests import test_execution_session as reference


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root, policy, execution, context = reference.fixture.__wrapped__(tmp_path, monkeypatch,
        SimpleNamespace(param={"role_isolation": True, "coverage_examination": True,
            "simulated_model_usage": True, "complete_packet_capture": True, "review_delivery": True}))
    tree = ast.parse(execution.executor._commands["researcher"][-1])
    printed = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "print")
    proposal = json_document(ast.literal_eval(printed.args[0]).encode())["proposal"]
    context = replace(context, grants=context.grants | {"review_page_operations.prepare", "review_page_operations.page",
        "review_page_session.assembled", "review_packet_execution.bind", "review_packet_execution.reserve", "coverage_observation.observe"})
    with context.activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(request_id="packet-admission", operation="strict.admit", arguments={"proposal": proposal})
        assert workspace.mutations.submit(request)["status"] == "committed"
    return root, policy, execution, context, proposal["inventory"]["claims"][0], tmp_path / "workers/complete-input.json"


def finish(session, attempt, prefix, *, retain_running=True):
    observation, reason = native._external(session, "invoke", attempt)
    assert reason is None
    for index in range(32):
        if observation["state"] != "running":
            break
        if retain_running:
            native.intake(session, observation, request_id=prefix + ".running." + str(index))
        observation, reason = native._external(session, "poll", attempt)
        assert reason is None
    assert observation["state"] == "succeeded" and observation["final_accounting"]
    return observation


def reserve(session, claim, request_id="review"):
    value = assembled(session, request_id="complete-pages", claim_id=claim["id"], page_chars=1024)
    attempt = reserve_evaluator(session, request_id=request_id, assembly_request_id="complete-pages",
        original_id="q", claim_id=claim["id"], config_ref="profile-a")
    assert attempt.input_document["complete_review_delivery"]["packet_sha256"] == value["packet_sha256"]
    return attempt


def reviews(root):
    config = yaml.safe_load((root / "research.yml").read_bytes())
    with owner("_evidence_usage").current_view(root, config) as view:
        return json.loads(json.dumps(view.state.strict_reviews))


def test_actual_child_receives_every_required_page_and_native_review_application(configured):
    root, policy, execution, context, claim, captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="packet-delivery", execution=execution) as session:
            attempt = reserve(session, claim)
            observation = finish(session, attempt, "delivery")
            native.intake(session, observation, request_id="delivery.final")
            child = json_document(captured.read_bytes())
            assert child == attempt.worker_document()
            assert hashlib.sha256(canonical(child["input"])).hexdigest() == attempt.input_sha256
            delivery = child["input"]["complete_review_delivery"]
            assert delivery["packet"]["complete"]["complete"] and not delivery["accepted"]
            assert delivery["worker_comprehension"] == "not_observed"
            assert {row["page_id"] for row in delivery["packet"]["header"]["pages"]} == {row["page_id"] for row in delivery["packet"]["pages"]}
            assert any(row.get("section_id") == "mechanical-counter" for row in delivery["packet"]["pages"])
            visible = json.dumps(child)
            for secret in ("owner_snapshot_sha256", "order_seed", "signing_key", "first_pass_refs", "credential_refs"):
                assert secret not in visible
            native._apply(session, attempt.attempt_id)
            records = execution.records(session)
            assert any(row["kind"] == "execution_applied" and row["data"]["attempt_id"] == attempt.attempt_id for row in records)
            assert len([row for row in records if row["kind"] == "execution_application" and row["data"]["operation"] == "evaluate"]) == 1
            native._apply(session, attempt.attempt_id)
            assert execution.records(session) == records
            # One native registration is a protocol receipt; this simulated
            # reference process does not establish genuine model semantics.
            assert len(reviews(root)) == 1


@pytest.mark.parametrize("change", ["material", "withdrawal"])
def test_actual_completed_worker_cannot_sign_after_current_owner_change(configured, monkeypatch, change):
    root, policy, execution, context, claim, _captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="stale-packet", execution=execution) as session:
            attempt = reserve(session, claim)
            observed = finish(session, attempt, "stale")
            native.intake(session, observed, request_id="stale.final")
            before = reviews(root)
            if change == "material":
                config = yaml.safe_load((root / "research.yml").read_bytes())
                path = root / config["sources"]["normalized_dir"] / (owner("normalize_sources").safe_source_id(claim["evidence"][0]["source_id"]) + ".md")
                path.write_bytes(path.read_bytes() + b"\nChanged decisive retained source.\n")
            else:
                usage = owner("_evidence_usage")
                config = yaml.safe_load((root / "research.yml").read_bytes())
                with usage.current_view(root, config) as view:
                    binding, checkpoint, state_id = view.state.binding, view.state.checkpoint, view.state.state_id
                from tests._execution_fixture import authenticate
                command = authenticate({"schema_version": "evidence-usage-command/v1", "state_id": state_id,
                    "workspace_binding": binding, "request_id": "withdraw-pages", "expected_checkpoint": checkpoint,
                    "action": "revoke", "body": {"source_id": claim["evidence"][0]["source_id"], "scope": "source",
                        "source_revision": None, "reason": "owner-withdrawal"}}, "owner", "revocation")
                usage.transact(root, config, command)
            monkeypatch.setattr(HostReviewSigner, "registration", lambda *_args, **_kwargs: pytest.fail("Stale decisive pages reached signing"))
            with pytest.raises(EvidenceWikiError):
                native._apply(session, attempt.attempt_id)
            assert reviews(root) == before


def test_omitted_required_page_changes_attempt_identity_and_cannot_enter_native_intake(configured, monkeypatch):
    root, policy, execution, context, claim, _captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="omitted-page", execution=execution) as session:
            attempt = reserve(session, claim)
            changed = attempt.input_document
            changed["complete_review_delivery"]["packet"]["pages"].pop()
            job = attempt.job
            job["packet_sha256"] = hashlib.sha256(canonical(changed)).hexdigest()
            altered = ExecutionAttempt.create(attempt_id=attempt.attempt_id, nonce=attempt.nonce, run_id=attempt.run_id,
                binding_id=attempt.binding_id, profile=execution.profile, config_ref=attempt.config_ref, job=job, input_document=changed)
            observation = finish(session, altered, "omitted", retain_running=False)
            assert execution.authenticate(observation, altered) == observation
            monkeypatch.setattr(HostReviewSigner, "registration", lambda *_args, **_kwargs: pytest.fail("An omitted page reached signing"))
            with pytest.raises(EvidenceWikiError):
                native.intake(session, observation, request_id="omitted.final")
            assert not reviews(root)
            assert not any(row["kind"] == "execution_application" for row in execution.records(session))


def test_actual_interrupted_page_receipt_set_cannot_reserve_evaluator(configured, monkeypatch):
    root, policy, execution, context, claim, _captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="partial-pages", execution=execution) as session:
            original = native.append
            seen = 0
            def interrupted(bound, kind, request_id, data):
                nonlocal seen
                if kind == "execution_review_page_delivery":
                    seen += 1
                    if seen == 2:
                        raise RuntimeError("Interrupted before required page receipt")
                return original(bound, kind, request_id, data)
            with monkeypatch.context() as fault:
                fault.setattr(native, "append", interrupted)
                with pytest.raises(RuntimeError, match="required page receipt"):
                    assembled(session, request_id="partial", claim_id=claim["id"], page_chars=1024)
            records = execution.records(session)
            assert len([row for row in records if row["kind"] == "execution_review_page_delivery"]) == 1
            with pytest.raises(EvidenceWikiError) as refused:
                reserve_evaluator(session, request_id="no-review", assembly_request_id="partial",
                    original_id="q", claim_id=claim["id"], config_ref="profile-a")
            assert refused.value.details.get("reason") == "complete_assembly_owner_receipt_required"
            assert execution.records(session) == records
            assert not any(row["kind"] == "execution_attempt" for row in records)


def test_enabled_profile_refuses_direct_evaluator_without_delivery_before_reservation(configured):
    root, policy, execution, context, claim, _captured = configured
    assert execution.profile.document()["schema_version"] == "research-execution-profile/v7"
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="direct-packet", execution=execution) as session:
            with native.gate(session):
                native._begin(session)
                records = execution.records(session)
                with pytest.raises(EvidenceWikiError) as refused:
                    native.reserve(session, "direct-review", {"operation": "evaluate", "original_id": "q",
                        "claim_id": claim["id"], "config_ref": "profile-a"}, native.current(session))
                assert refused.value.error_code.endswith("COMPLETE_REVIEW_DELIVERY_REQUIRED")
                assert execution.records(session) == records


def test_automatic_profile_requires_complete_delivery_for_both_first_pass_reviewers(configured):
    root, policy, execution, context, _claim, _captured = configured
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="automatic-packets", execution=execution) as session:
            for index in range(16):
                before = session.current()
                value = session.advance(request_id="automatic-" + str(index), expected_basis_id=before["basis_id"])
                records = execution.records(session)
                reviews = [row for row in records if row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate"]
                if len(reviews) == 2:
                    break
                assert not value["terminal"], value
            else:
                pytest.fail("Both configured first-pass reviewers were not applied within the bound.")
            attempts = [row["data"] for row in records if row["kind"] == "execution_attempt" and row["data"]["attempt"]["job"]["operation"] == "evaluate"]
            assert {row["attempt"]["config_ref"] for row in attempts} == {"profile-a", "profile-b"}
            for row in attempts:
                assert row["review_assembly_request_id"]
                parent = row["attempt"]["input"]
                assert parent["complete_review_delivery"]["packet"]["complete"]["complete"]
                assert parent["packet"]["context_id"] == parent["complete_review_delivery"]["packet"]["header"]["context_id"]
