"""Real process interruption at retained fetch and native review boundaries."""

import multiprocessing
import os
from dataclasses import replace

import pytest

from evidence_wiki import Workspace
from evidence_wiki import execution_session as native
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.pack_discovery import owner
from tests import test_acquisition_workflow_adversarial as acquisition_cases
from tests import test_execution_session as execution_cases
from tests.test_acquisition_dispatch import opener
from tests.test_native_capture_revision import metadata


class SharedCalls:
    def __init__(self, count):
        self.count = count

    def append(self, url):
        assert url == "https://example.org/source"
        with self.count.get_lock():
            self.count.value += 1


def fetch_then_hard_exit(root, policy, execution, context, parameters):
    append = native.append
    def interrupt(session, kind, request_id, data):
        if kind == "execution_acquisition_workflow_result":
            os._exit(73)
        return append(session, kind, request_id, data)
    native.append = interrupt
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="retained-fetch", execution=execution) as session:
            session.acquire_evidence(**parameters)


def test_real_process_retains_completed_fetch_and_fulfillment_without_refetch(tmp_path, monkeypatch):
    if not hasattr(os, "fork"):
        pytest.skip("Reference handoff requires native fork")
    root, policy, execution, context = acquisition_cases.configured.__wrapped__(tmp_path, monkeypatch)
    count = multiprocessing.get_context("fork").Value("i", 0)
    transport = owner("_acquisition_transport")
    monkeypatch.setattr(transport, "execute_planned_request", opener(transport.execute_planned_request,
        b"Measured reflectance was 0.74 ratio with retained original context.\n", SharedCalls(count),
        root, context.mutation_control_directory))
    with context.activate(), Workspace.open(root) as workspace:
        source = workspace.source_requests.add(kind="web", query_or_identifier="https://example.org/source",
            rationale="Need scoped retained original", question_slugs=["q"], scope={"facet_id":"primary"})["request"]
    parameters = {"request_id":"fetch", "source_request_id":source["request_id"], "expected_request_basis":digest(source),
        "metadata":metadata("plain_text", request_id=source["request_id"], scope={"facet_id":"primary"},
            origin_url="https://example.org/source"), "path":"raw/web/recovered.txt"}
    process = multiprocessing.get_context("fork").Process(target=fetch_then_hard_exit,
        args=(root, policy, execution, context, parameters))
    process.start()
    process.join(timeout=60)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Reference child did not reach its retained fetch boundary")
    assert process.exitcode == 73 and count.value == 1
    monkeypatch.setattr(transport, "execute_planned_request", lambda *_args, **_kwargs: pytest.fail("A completed fetch was repeated"))
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="retained-fetch", execution=execution) as session:
            records = execution.records(session)
            assert not any(row["kind"] == "execution_acquisition_workflow_result" for row in records)
            assert workspace.source_requests.current(request_ids=[source["request_id"]])["requests"][0]["recorded_status"] == "fulfilled"
            original = native.deadline(session)
            result = session.acquire_evidence(**parameters)
            assert result["status"] == "fulfilled" and count.value == 1
            assert native.deadline(session) == original
            assert session.acquire_evidence(**parameters) == result
            assert len([row for row in execution.records(session) if row["kind"] == "execution_acquisition_workflow_result"]) == 1


def finish_after_review(root, policy, execution, context, run_id, queue):
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id=run_id, execution=execution) as session:
            value = session.recovery_status()
            result = session.run_until_terminal(request_id="resume-drive", expected_basis_id=value["current_basis_id"], max_steps=64)
            queue.put({"pid":os.getpid(), "result":result, "deadline":native.deadline(session).isoformat(),
                "binding":session.binding, "records":execution.records(session)})


def test_real_second_process_after_review_reaches_native_terminal_without_replaying_review(tmp_path, monkeypatch):
    root, policy, execution, context = execution_cases.fixture.__wrapped__(tmp_path, monkeypatch)
    context = replace(context, grants=context.grants | {"research_session.recovery_status", "research_session.reconcile_recovery"})
    with context.activate(), Workspace.open(root) as workspace:
        with workspace.research.session(policy, run_id="review-restart", execution=execution) as session:
            for index in range(32):
                current = session.current()
                step = session.advance(request_id="before-restart-" + str(index), expected_basis_id=current["basis_id"])
                if any(row["kind"] == "execution_applied" and row["data"].get("operation") == "evaluate" for row in execution.records(session)):
                    break
                assert not step["terminal"]
            else:
                pytest.fail("Native reference did not reach a completed review")
            binding = session.binding
            deadline = native.deadline(session).isoformat()
            attempts = [row["data"]["attempt"] for row in execution.records(session) if row["kind"] == "execution_attempt"]
    processes = multiprocessing.get_context("fork")
    queue = processes.Queue()
    child = processes.Process(target=finish_after_review, args=(root, policy, execution, context, "review-restart", queue))
    child.start()
    try:
        value = queue.get(timeout=180)
        child.join(timeout=10)
        assert child.exitcode == 0 and value["pid"] != os.getpid()
        assert value["binding"] == binding and value["deadline"] == deadline
        assert value["result"]["terminal"] and value["result"]["outcome"] == "accepted"
        final_attempts = [row["data"]["attempt"] for row in value["records"] if row["kind"] == "execution_attempt"]
        assert final_attempts == attempts
        with context.activate(), Workspace.open(root) as workspace:
            with workspace.research.session(policy, run_id="review-restart", execution=execution) as session:
                original = execution.records(session)
                terminal = session.reconcile_recovery(request_id="read-terminal", expected_basis_id=session.current()["basis_id"])
                assert terminal["terminal"] and execution.records(session) == original
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
