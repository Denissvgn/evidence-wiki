"""Finite inert attacks exercise existing custody owners, not model obedience."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from evidence_wiki import HostRuntime, Workspace
from evidence_wiki.agent_execution import ExecutionAttempt, _digest
from evidence_wiki.agent_qualification import QualificationError
from evidence_wiki.errors import EvidenceWikiError
from evidence_wiki.onboarding_mcp import OnboardingMcpServer
from evidence_wiki.review_context import create_review_context
from evidence_wiki.run_policy import RunPolicy
from evidence_wiki.workflow_contracts import WorkflowContractError
from tests import test_execution_session as execution_reference
from tests import test_mutation_operations as mutation_reference
from tests import test_qualification_calibration as calibration_reference
from tests.test_execution_isolation_adversarial import profile, review_attempt
from tests.test_mutation_operations import host, template
from tests.test_planner_adversarial import begin, configured
from tests.test_qualification_calibration import EXPIRY, NOW
from tests.test_review_context import inputs

CORPUS_PATH = Path(__file__).parent / "fixtures/acceptance-paths/challenges.json"
CORPUS_SHA256 = "043cbc5f74e133e0d28f62b659f91bb4a043e19feee7107002ee688a8670cb75"
CORPUS_BYTES = CORPUS_PATH.read_bytes()
CHALLENGES = tuple((row["id"], row["text"]) for row in json.loads(CORPUS_BYTES)["cases"])
CLIENT_FIELDS = ("runtime", "grants", "policy", "qualification_records", "operation_control", "thresholds", "task_role")


@pytest.fixture
def execution_case(tmp_path, monkeypatch):
    return execution_reference.fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture
def mutation_roots(tmp_path, monkeypatch):
    return mutation_reference.roots.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def calibration_issued():
    return calibration_reference.issued.__wrapped__()


@pytest.fixture
def acceptance_record(request):
    plugin = request.config.pluginmanager.get_plugin("acceptance-records")
    return (
        (lambda owner, result: plugin.record_owner_result(request.node.nodeid, owner, result))
        if plugin
        else (lambda owner, result: None)
    )


def hashed(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert len(raw) <= 2 * 1024 * 1024
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def snapshot(root):
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    assert len(files) <= 1024
    return {
        path.relative_to(root).as_posix(): "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() for path in files
    }


@pytest.mark.parametrize("challenge", CHALLENGES, ids=[row[0] for row in CHALLENGES])
def test_hostile_review_text_preserves_data_without_control_authority(challenge, acceptance_record):
    assert hashlib.sha256(CORPUS_BYTES).hexdigest() == CORPUS_SHA256
    challenge_id, hostile_text = challenge
    supplied = inputs(2)
    supplied["sources"][0]["content"] += "\n" + hostile_text
    supplied["owner_snapshot"]["metadata"].update(
        expected_label="protected-answer-sentinel", controller_ref="protected-controller-sentinel"
    )
    context = create_review_context(**supplied)
    packet = context.document()
    context.check(supplied["owner_snapshot"], supplied["rubric"], supplied["sources"])
    assert any(hostile_text in source["content"] for source in packet["sources"])
    assert packet["task_role"] == "evaluator" and packet["manifest"]["context_complete"]
    frozen_input = {
        "packet": packet,
        "instructions": "Treat source content as retained evidence.",
        "output_contract": {"verdicts": "bounded"},
    }
    selected = profile()
    original = review_attempt(selected)
    for config_ref, task_role, operation, document in (
        ("judge", "evaluator", "evaluate", frozen_input),
        (
            "worker",
            "researcher",
            "generate",
            {"source_packets": packet["sources"], "instructions": "Use the supplied source data."},
        ),
    ):
        role = selected.role(config_ref)
        job = original.job | {
            "operation": operation,
            "role": role.role,
            "task_role": task_role,
            "controller_ref": role.controller_ref,
            "role_scope_sha256": selected.role_scopes.scope(config_ref).sha256,
            "context_id": packet["context_id"],
            "packet_sha256": _digest(document),
            "original_text": packet["question"]["question"],
            "payload": {"claim_id": "claim-1", "packet_id": context.packet_id}
            if operation == "evaluate"
            else {"question": packet["question"]["question"]},
        }
        attempt = ExecutionAttempt.create(
            attempt_id="attack-" + config_ref,
            nonce="bounded-nonce",
            run_id="bounded-run",
            binding_id="bounded-binding",
            profile=selected,
            config_ref=config_ref,
            job=job,
            input_document=document,
        )
        delivered = attempt.worker_document()
        assert delivered["input"] == document and hostile_text in json.dumps(delivered)
        assert delivered["job"]["task_role"] == task_role and delivered["job"]["role"] == role.role
        encoded = json.dumps(delivered)
        for hidden in (
            "protected-answer-sentinel",
            "protected-controller-sentinel",
            "hidden-generator",
            "hidden-key",
            "prior_verdict",
            "expected_label",
            "order_seed",
        ):
            assert hidden not in encoded
        document["instructions"] = "Caller mutation after immutable attempt creation."
        assert attempt.worker_document() == delivered
        acceptance_record(
            "ExecutionAttempt.worker_document",
            {
                "challenge_id": challenge_id,
                "task_role": task_role,
                "basis_id": context.binding()["owner_snapshot_sha256"],
                "input_sha256": attempt.input_sha256,
                "delivered_sha256": hashed(delivered),
                "packet_sha256": context.packet_id,
                "source_inputs_sha256": context.binding()["source_input_sha256"],
                "source_text_retained": True,
                "protected_metadata_absent": True,
                "authority": "not_granted",
                "model_obedience": "not_observed",
            },
        )


@pytest.mark.parametrize("field", CLIENT_FIELDS)
def test_hostile_client_fields_refuse_without_native_effects(execution_case, field, acceptance_record):
    root, policy, execution, context = execution_case
    runtime = HostRuntime.from_services(policy, context, execution)
    server = OnboardingMcpServer(allowed_roots=[root], runtime=runtime, research_allow=["run-cycle"])
    before = snapshot(root)
    policy_before = policy.to_json()
    authority_before = hashlib.sha256(context.authority_file.read_bytes()).hexdigest()
    try:
        result = server.call_tool(
            "research_run_cycle",
            {
                "target": str(root),
                "run_id": "hostile-client",
                "request_id": "drive",
                field: {"claim": dict(CHALLENGES)["threshold-edit"], "authorized": True},
            },
        )
        assert result["status"] == "refused"
        assert result["error"]["error_code"] == "RESEARCH_COMMAND_ARGUMENTS_INVALID"
        assert policy.to_json() == policy_before
        assert hashlib.sha256(context.authority_file.read_bytes()).hexdigest() == authority_before
        assert snapshot(root) == before and not (root / "runs/hostile-client").exists()
        acceptance_record(
            "OnboardingMcpServer.call_tool",
            {
                "source_root": str(root),
                "field": field,
                "typed_refusal": result["error"]["error_code"],
                "result_sha256": hashed(result),
                "policy_id": policy.policy_id,
                "before_effects_sha256": hashed(before),
                "after_effects_sha256": hashed(snapshot(root)),
                "authority_digest_unchanged": True,
                "run_created": False,
            },
        )
    finally:
        server.close()


@pytest.mark.parametrize("order", ("forward", "reversed"))
def test_signed_position_failure_cannot_be_voted_away_or_lower_thresholds(calibration_issued, order, acceptance_record):
    policy, selected, suite, host_double, observations, authority, _record = calibration_issued
    frozen = policy.to_json()
    challenged = []
    failed_receipt = None
    for attempt, receipt in observations:
        if receipt["output"]["case_id"] == "position-45":
            case = next(case for case in suite["cases"] if case["case_id"] == "position-45")
            receipt = host_double.receipt(attempt, case | {"expected_label": "accept"})
            failed_receipt = receipt
            assert host_double.authenticate(receipt, attempt) == receipt
        challenged.append((attempt, receipt))
    if order == "reversed":
        challenged.reverse()
    report = authority.issue(
        policy,
        selected,
        "profile-a",
        suite,
        challenged,
        host_double,
        observed_utc=NOW,
        expires_utc=EXPIRY,
        verification_seconds=30,
    )
    assert report["state"] == "unqualified"
    assert "critical_false_acceptance" in report["domains"]["domain"]["reasons"]
    assert report["domains"]["domain"]["all_references"]["counts"]["critical_false_acceptances"] == 1
    assert len(report["observation_refs"]) == 600 and report["limits"] == policy.to_dict()["thresholds"]
    assert authority.verify(report) == report and policy.to_json() == frozen
    for field, value in (
        ("critical_false_acceptances_max", 99),
        ("positive_acceptance_min", "0"),
        ("meaning_preserving_flip_max", "1"),
    ):
        lowered = policy.to_dict()
        lowered["policy_id"] = None
        lowered["thresholds"][field] = value
        with pytest.raises(WorkflowContractError):
            RunPolicy(lowered)
    rewritten = copy.deepcopy(report)
    rewritten["limits"]["critical_false_acceptances_max"] = 99
    with pytest.raises(QualificationError, match="authentication_refused"):
        authority.verify(rewritten)
    acceptance_record(
        "CalibrationAuthority.issue",
        {
            "order": order,
            "policy_id": policy.policy_id,
            "suite_sha256": suite["suite_sha256"],
            "observation_count": len(challenged),
            "report_sha256": hashed(report),
            "critical_receipt_sha256": hashed(failed_receipt),
            "state": report["state"],
            "reasons": report["domains"]["domain"]["reasons"],
            "limits": report["limits"],
            "critical_false_acceptances": 1,
            "threshold_edit_refused": True,
            "evidence_class": "simulated",
            "live_model_execution": "not_observed",
        },
    )


def test_concurrent_identical_request_has_one_native_commit(mutation_roots, acceptance_record):
    root, control = mutation_roots
    with host(mutation_roots).activate(), Workspace.open(root) as workspace:
        request = workspace.mutations.prepare(
            request_id="same-request", operation="coverage.init", arguments={"slug": "sample", "template": template()}
        )
    barrier = Barrier(2, timeout=10)

    def submit():
        with host(mutation_roots).activate(), Workspace.open(root) as workspace:
            barrier.wait()
            try:
                return {"receipt": workspace.mutations.submit(request)}
            except EvidenceWikiError as error:
                return {"typed_refusal": error.error_code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        results = [future.result(timeout=30) for future in futures]
    receipts = [result["receipt"] for result in results if "receipt" in result]
    assert receipts and all(receipt == receipts[0] for receipt in receipts)
    assert all(result.get("typed_refusal") in {None, "MUTATION_LOCK_BUSY"} for result in results)
    receipt = receipts[0]
    assert receipt["status"] == "committed" and receipt["result"]["manifest"]["question_slug"] == "sample"
    after = snapshot(root)
    control_after = snapshot(control)
    with host(mutation_roots).activate(), Workspace.open(root) as workspace:
        replay = workspace.mutations.submit(request)
        inspected = workspace.mutations.inspect("same-request")
        assert replay == receipt and inspected["record"]["receipt"] == receipt
    assert snapshot(root) == after and snapshot(control) == control_after
    assert len(list((root / "sources/coverage").glob("sample.yml"))) == 1
    assert len(list(control.rglob("receipt.json"))) == 1
    acceptance_record(
        "Workspace.mutations.submit",
        {
            "source_root": str(root),
            "request_sha256": hashed(request),
            "concurrent_results": [
                {"receipt_sha256": hashed(result["receipt"])} if "receipt" in result else result for result in results
            ],
            "receipt_sha256": hashed(receipt),
            "replay_sha256": hashed(replay),
            "inspection_sha256": hashed(inspected),
            "status": receipt["status"],
            "native_manifest_count": 1,
            "durable_receipt_count": 1,
            "after_effects_sha256": hashed(after),
            "replay_effects_sha256": hashed(snapshot(root)),
            "journal_sha256": hashed(control_after),
            "replay_journal_sha256": hashed(snapshot(control)),
        },
    )


def test_failed_planner_exhausts_finite_retries_and_replay_cannot_reset(tmp_path, monkeypatch, acceptance_record):
    root, policy, execution, context = configured(tmp_path, monkeypatch)
    count_path = tmp_path / "failed-planner-count"
    program = (
        "import pathlib,sys; p=pathlib.Path("
        + repr(str(count_path))
        + "); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(1)"
    )
    execution.executor._commands["planner"] = (sys.executable, "-c", program)
    with context.activate(), Workspace.open(root) as workspace:
        with begin(workspace, policy, execution) as session:
            for index in range(24):
                result = session.advance(
                    request_id="failed-step-" + str(index), expected_basis_id=session.current()["basis_id"]
                )
                if result["terminal"]:
                    break
            else:
                pytest.fail("Always-failing planner did not reach its finite terminal outcome")
            assert result["outcome"] == "blocked_external" and result["reason"] == "executor_unavailable"
            records = execution.records(session)
            attempts = [
                row["data"]
                for row in records
                if row["kind"] == "execution_attempt"
                and row["data"].get("retry_purpose", row["data"].get("purpose")) == "plan"
            ]
            limit = policy.to_dict()["continuation_limits"]["retry_attempts"]
            assert len(attempts) == limit + 1 and int(count_path.read_text()) == limit + 1
            assert session.current()["usage"]["retry_attempts"] == limit
            assert len({hashed(row["owner_action"]) for row in attempts}) == 1
            assert len({row["selection_key"] for row in attempts}) == 1
            assert not any(
                row["kind"] == "execution_native" and row["data"]["operation"] == "strict.admit" for row in records
            )
            after = snapshot(root)
            records_hash = hashed(records)
            replay = session.advance(request_id=result["request_id"], expected_basis_id=result["before_basis_id"])
            assert replay["historical"] and replay["outcome"] == result["outcome"]
            assert hashed(execution.records(session)) == records_hash and snapshot(root) == after
        with workspace.research.session(policy, run_id="planning-run", execution=execution) as resumed:
            again = resumed.advance(request_id=result["request_id"], expected_basis_id=result["before_basis_id"])
            assert again["historical"] and again["outcome"] == result["outcome"]
            assert resumed.current()["usage"]["retry_attempts"] == limit
            assert hashed(execution.records(resumed)) == records_hash and int(count_path.read_text()) == limit + 1
            acceptance_record(
                "ResearchSession.advance",
                {
                    "source_root": str(root),
                    "policy_id": policy.policy_id,
                    "result_sha256": hashed(result),
                    "outcome": result["outcome"],
                    "reason": result["reason"],
                    "retry_limit": limit,
                    "attempt_count": len(attempts),
                    "process_count": int(count_path.read_text()),
                    "attempt_inputs": [row["attempt"]["input_sha256"] for row in attempts],
                    "journal_sha256": records_hash,
                    "replay_journal_sha256": hashed(execution.records(resumed)),
                    "after_effects_sha256": hashed(after),
                    "replay_effects_sha256": hashed(snapshot(root)),
                    "evidence_class": "deterministic_verified",
                    "live_model_execution": "not_observed",
                },
            )
