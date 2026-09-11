"""Enumerated acquisition histories compare durable state with a small oracle."""

import contextlib
import json
from dataclasses import dataclass, field
from unittest import mock

import pytest

from tests import test_contingent_bookkeeping_baseline as bookkeeping
from tests import test_cumulative_acquisition as cumulative
from tests import test_delegated_acquisition_e2e as protocol
from tests import test_upgrade_workspace as upgrade


@dataclass
class AcquisitionModel:
    issued: int = 0
    completed: int = 0
    pending: str | None = None
    fulfilled: dict[str, str] = field(default_factory=dict)
    reopened: bool = False

    def check(self, workspace, requests):
        session = bookkeeping.session_document(workspace)
        assert session["action_count"] == self.issued
        assert session["window_action_count"] == self.issued
        assert session["completed_action_count"] == self.completed
        assert session["pending_action_id"] == self.pending
        assert len(session["child_run_ids"]) == self.issued
        assert len(set(session["child_run_ids"])) == self.issued
        for request_id in requests:
            record = bookkeeping.stored_request(workspace, request_id)
            assert record["status"] == ("fulfilled" if request_id in self.fulfilled else "open")
            assert record.get("source_id") == self.fulfilled.get(request_id)
        page = bookkeeping.question_fields(workspace)
        assert page["status"] == ("open" if self.reopened else "blocked")
        if self.reopened:
            assert not page.get("blocking_request_ids")
            assert set(page["source_ids"]) == set(self.fulfilled.values())
        else:
            assert page["blocking_request_ids"] == requests


def protected_bytes(workspace):
    paths = [workspace / "sources/source-requests.jsonl", workspace / "log.md",
             workspace / "research.yml", workspace / "scripts/query_index.py"]
    for relative in ("wiki/questions", "runs/orchestrations", "runs/order-claims"):
        paths.extend(path for path in (workspace / relative).rglob("*") if path.is_file())
    return {path.relative_to(workspace).as_posix(): path.read_bytes() for path in paths}


@pytest.mark.parametrize("delegated", [False, True], ids=["provider", "delegated"])
@pytest.mark.parametrize("ending", ["commit", "interrupt-first", "interrupt-second", "failed"])
def test_acquisition_history_preserves_progress_and_replay_budget(tmp_path, delegated, ending):
    driver = cumulative.CumulativeAcquisitionTests()
    workspace, requests = driver.workspace_with_blockers(tmp_path, delegated)
    trace = []
    initial = {
        "fixture": "one blocked question with two required normalized observations",
        "delegated": delegated,
        "ending": ending,
        "requests": bookkeeping.stored_requests(workspace),
        "question": (workspace / "wiki/questions" / f"{protocol.QUESTION_SLUG}.md").read_text(),
        "configuration": (workspace / "research.yml").read_text(),
    }
    model = AcquisitionModel()

    @contextlib.contextmanager
    def step(operation, **arguments):
        trace.append({"operation": operation, **arguments})
        try:
            yield
        except BaseException as error:
            error.add_note(json.dumps({"initial_fixture": initial, "operations": trace}, indent=2))
            raise

    with step("start", max_actions=2):
        code, result = driver.controller(workspace, "start", "--orchestration-id",
                                        protocol.ORCHESTRATION_ID, "--agent-id", "pm-agent",
                                        "--max-actions", "2")
        assert code == 0, result
        model.check(workspace, requests)

    for number, request_id in enumerate(requests, 1):
        if not delegated:
            with step("select-candidate", request_id=request_id, observation=number):
                driver.select(workspace, request_id, number)
        with step("issue", observation=number):
            order = driver.pending_order(workspace)
            model.issued += 1
            model.pending = order["action_id"]
            model.check(workspace, requests)
            if number == 2:
                assert order["scope"]["request_ids"] == [requests[1]]

        for repeat in range(2):
            with step("replay-pending", repeat=repeat, action_id=order["action_id"]):
                code, replay = driver.controller(workspace, "next", "--orchestration-id",
                                                 protocol.ORCHESTRATION_ID, "--resume")
                assert code == 0, replay
                assert replay == order
                model.check(workspace, requests)

        if number == 1:
            for dry_run in (True, False):
                with step("upgrade-pending", dry_run=dry_run):
                    before = protected_bytes(workspace)
                    args = ["upgrade", "--target", str(workspace)]
                    if dry_run:
                        args.append("--dry-run")
                    code, stdout, stderr = upgrade.run_cli_result(*args)
                    assert code == 2, (stdout, stderr)
                    assert "UPGRADE_PENDING_ORDER" in stderr
                    assert order["action_id"] in stderr
                    assert protected_bytes(workspace) == before
                    model.check(workspace, requests)

        with step("deliver-and-file-claim", request_id=request_id, observation=number):
            source_id, artifact = driver.deliver(workspace, request_id, number, order, delegated)
            model.check(workspace, requests)
            claims = bookkeeping.claim_ledger(workspace, order["action_id"])
            assert list(claims["fulfilments"]) == [request_id]
            assert claims["fulfilments"][request_id]["source_id"] == source_id

        with step("repeat-fulfilment", request_id=request_id, source_id=source_id):
            before = protected_bytes(workspace)
            driver.run_script(protocol.REQUESTS, ["fulfill", "--request-id", request_id,
                                                 "--source-id", source_id], workspace)
            assert protected_bytes(workspace) == before
            model.check(workspace, requests)

        if ending == "failed":
            with step("fail-delivered-order", action_id=order["action_id"]):
                code, result = driver.submit(workspace, order["action_id"], outcome="failed",
                                             artifacts=[artifact])
                assert code == protocol.CONTROLLER.EXIT_INVALID, result
                assert result["status"] == "failed"
                model.pending = None
                model.completed += 1
                model.check(workspace, requests)
            for repeat in range(2):
                with step("resume-terminal-failure", repeat=repeat):
                    before = protected_bytes(workspace)
                    code, result = driver.controller(workspace, "next", "--orchestration-id",
                                                      protocol.ORCHESTRATION_ID, "--resume")
                    assert code == protocol.CONTROLLER.EXIT_INVALID, result
                    assert result["status"] == "failed"
                    assert protected_bytes(workspace) == before
                    model.check(workspace, requests)
            return

        with step("reopen", request_id=request_id):
            before = protected_bytes(workspace)
            code, result = driver.reopen(workspace, request_id, source_id)
            if number == 1:
                assert code == 2, result
                assert result["error_code"] == "QUESTION_BLOCKERS_UNFULFILLED"
                assert protected_bytes(workspace) == before
            else:
                assert code == 0, result
                claim = bookkeeping.claim_ledger(workspace, order["action_id"])["reopens"]
                assert set(claim[protocol.QUESTION_SLUG]["source_ids"]) == {
                    *model.fulfilled.values(), source_id}
            model.check(workspace, requests)

        if delegated and number == 1:
            with step("record-unavailable-sibling", request_id=requests[1]):
                driver.record_failure(workspace, requests[1], "provider_throttled", order["action_id"])
                model.check(workspace, requests)

        interrupt = ending == ("interrupt-first" if number == 1 else "interrupt-second")
        if interrupt:
            real_write = protocol.CONTROLLER.write_json_atomic
            session_path = protocol.CONTROLLER.session_path(workspace.resolve(), protocol.ORCHESTRATION_ID)
            interrupted = False

            def interrupt_parent_commit(
                path, document, *, expected_path=session_path,
                expected_action=order["action_id"], write=real_write,
            ):
                nonlocal interrupted
                if (not interrupted and path == expected_path
                        and document.get("last_completed_action_id") == expected_action):
                    interrupted = True
                    raise protocol.CONTROLLER.OrchestrationControllerError(
                        "INJECTED_COMMIT_INTERRUPTION", "Interrupted before the parent session commit.")
                write(path, document)

            with step("interrupt-after-bookkeeping", action_id=order["action_id"]):
                with mock.patch.object(protocol.CONTROLLER, "write_json_atomic", interrupt_parent_commit):
                    code, result = driver.submit(workspace, order["action_id"], artifacts=[artifact])
                assert interrupted
                assert code == protocol.CONTROLLER.EXIT_INVALID, result
                assert result["error_code"] == "INJECTED_COMMIT_INTERRUPTION"
                model.fulfilled[request_id] = source_id
                model.reopened = number == 2
                model.check(workspace, requests)
                assert protocol.CONTROLLER.work_result_path(
                    workspace, protocol.ORCHESTRATION_ID, order["action_id"]).is_file()

        with step("commit-or-retry", action_id=order["action_id"]):
            code, result = driver.submit(workspace, order["action_id"], artifacts=[artifact])
            assert code == 0, result
            model.fulfilled[request_id] = source_id
            model.reopened = number == 2
            model.completed += 1
            model.pending = None
            model.check(workspace, requests)

        for repeat in range(2):
            with step("repeat-completed-submit", repeat=repeat, action_id=order["action_id"]):
                before = protected_bytes(workspace)
                code, result = driver.submit(workspace, order["action_id"], artifacts=[artifact])
                assert code == 0, result
                assert protected_bytes(workspace) == before
                model.check(workspace, requests)

    with step("exhaust-action-budget"):
        code, result = driver.next_action(workspace)
        assert code == protocol.CONTROLLER.EXIT_PAUSED, result
        assert result["status"] == "paused"
        assert "max_actions" in result["pause_reason"]
        model.check(workspace, requests)


@pytest.mark.parametrize("delegated", [False, True], ids=["provider", "delegated"])
def test_fully_delivered_history_requires_explicit_question_reopen(tmp_path, delegated):
    driver = cumulative.CumulativeAcquisitionTests()
    workspace, requests = driver.workspace_with_blockers(tmp_path, delegated)
    if not delegated:
        driver.select(workspace, requests[0], 1)
    driver.start(workspace)
    first = driver.pending_order(workspace)
    source_one, artifact_one = driver.deliver(workspace, requests[0], 1, first, delegated)
    if delegated:
        driver.record_failure(workspace, requests[1], "provider_throttled", first["action_id"])
    code, result = driver.submit(workspace, first["action_id"], artifacts=[artifact_one])
    assert code == 0, result
    if not delegated:
        driver.select(workspace, requests[1], 2)
    second = driver.pending_order(workspace)
    _, artifact_two = driver.deliver(workspace, requests[1], 2, second, delegated)
    before = protected_bytes(workspace)

    code, result = driver.submit(workspace, second["action_id"], artifacts=[artifact_two])

    assert code == protocol.CONTROLLER.EXIT_INVALID, result
    assert result["error_code"] == "ORCHESTRATION_POSTCONDITION_FAILED", result
    assert [entry["question_slug"] for entry in result["details"]["question_transition_failures"]] == [protocol.QUESTION_SLUG]
    after = protected_bytes(workspace)
    for relative, content in before.items():
        if relative.startswith(("sources/", "wiki/", "runs/order-claims/")):
            assert after[relative] == content, relative
    assert bookkeeping.stored_request(workspace, requests[0])["source_id"] == source_one
    assert bookkeeping.stored_request(workspace, requests[1])["status"] == "open"
    assert bookkeeping.question_fields(workspace)["status"] == "blocked"
