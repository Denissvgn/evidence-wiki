import contextlib
import errno
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
RUN_CONTROLLER_PATH = SCRIPTS / "run_controller.py"

RUN_STATE_FIELDS = {
    "schema_version",
    "run_id",
    "started_at",
    "updated_at",
    "last_heartbeat_at",
    "agent_id",
    "handoff",
    "state",
    "state_history",
    "workspace_baseline",
    "question_counts",
    "source_counts",
    "candidate_counts",
    "coverage_counts",
    "budget_state",
    "budget_overrides",
    "failure_records",
    "recovery_history",
    "final_verdict",
}

ANSWERING_PATH = (
    "planned",
    "discovering",
    "candidates_ready",
    "fetch_planned",
    "fetching",
    "evidence_ready",
    "answering",
)


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"missing workspace script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RunControllerTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        init = load_script_module("run_controller_init", SCRIPTS / "init_research_workspace.py")
        target = root / "workspace"
        with contextlib.redirect_stdout(io.StringIO()):
            code = init.main(
                [
                    "--target",
                    str(target),
                    "--project-name",
                    "run-controller-workspace",
                    "--project-description",
                    "Workspace for run controller tests.",
                ]
            )
        self.assertEqual(0, int(code or 0))
        return target

    def load_controller(self):
        return load_script_module("run_controller_under_test", RUN_CONTROLLER_PATH)

    def run_module(self, module: Any, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def start_run(
        self,
        module: Any,
        target: Path,
        run_id: str = "run-2026-06-29T010203Z-test",
    ) -> dict[str, Any]:
        code, stdout, stderr = self.run_module(
            module,
            [
                "--project-root",
                str(target),
                "start",
                "--run-id",
                run_id,
                "--agent-id",
                "agent-pm",
                "--format",
                "json",
            ],
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        return json.loads(stdout)

    def transition(self, module: Any, target: Path, run_id: str, to_state: str) -> dict[str, Any]:
        code, stdout, stderr = self.run_module(
            module,
            [
                "--project-root",
                str(target),
                "transition",
                "--run-id",
                run_id,
                "--agent-id",
                "agent-pm",
                "--to-state",
                to_state,
                "--reason",
                f"Move to {to_state}.",
                "--format",
                "json",
            ],
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def events(self, target: Path, run_id: str) -> list[dict[str, Any]]:
        path = target / "runs" / run_id / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def rewrite_liveness_timestamps(self, target: Path, run_id: str, timestamp: str) -> None:
        state_path = target / "runs" / run_id / "run-state.json"
        document = json.loads(state_path.read_text(encoding="utf-8"))
        document["updated_at"] = timestamp
        document["last_heartbeat_at"] = None
        document["state"]["entered_at"] = timestamp
        for history in document["state_history"]:
            history["changed_at"] = timestamp
        state_path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")

        events_path = target / "runs" / run_id / "events.jsonl"
        events = self.events(target, run_id)
        for event in events:
            event["occurred_at"] = timestamp
        events_path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")

    def test_start_creates_run_state_events_and_baseline_artifacts(self):
        controller = self.load_controller()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            candidate_store = target / "sources" / "discovery" / "candidates.jsonl"
            candidate_store.parent.mkdir(parents=True, exist_ok=True)
            candidate_store.write_text(
                "\n".join(
                    [
                        json.dumps({"candidate_id": "cand-new"}),
                        json.dumps({"candidate_id": "cand-selected", "status": "selected"}),
                        json.dumps({"candidate_id": "cand-rejected", "status": "rejected"}),
                        json.dumps({"candidate_id": "cand-fetched", "status": "fetched"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            run_state = self.start_run(controller, target)

            run_id = "run-2026-06-29T010203Z-test"
            state_path = target / "runs" / run_id / "run-state.json"
            events_path = target / "runs" / run_id / "events.jsonl"
            self.assertTrue(state_path.is_file())
            self.assertTrue(events_path.is_file())
            self.assertEqual(RUN_STATE_FIELDS, set(run_state))
            self.assertEqual(run_state, json.loads(state_path.read_text(encoding="utf-8")))
            self.assertEqual("1.0", run_state["schema_version"])
            self.assertEqual(run_id, run_state["run_id"])
            self.assertEqual("agent-pm", run_state["agent_id"])
            self.assertIsNone(run_state["last_heartbeat_at"])
            self.assertEqual("initialized", run_state["state"]["current"])
            self.assertEqual(["planned", "failed"], run_state["state"]["allowed_next_states"])
            self.assertIsNone(run_state["state"]["blocking_reason"])
            self.assertEqual([], run_state["failure_records"])
            self.assertEqual([], run_state["recovery_history"])
            self.assertIsNone(run_state["final_verdict"])
            self.assertEqual(
                {"total": 4, "new": 1, "selected": 1, "rejected": 1, "fetched": 1},
                run_state["candidate_counts"],
            )
            self.assertEqual(0, run_state["coverage_counts"]["required"])
            self.assertEqual(run_state["question_counts"]["total"], run_state["coverage_counts"]["unknown"])

            baseline = run_state["workspace_baseline"]
            self.assertTrue((target / baseline["status_path"]).is_file())
            self.assertTrue((target / baseline["run_report_baseline_path"]).is_file())

            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len(events))
            self.assertEqual("evt-0001", events[0]["event_id"])
            self.assertEqual("state_transition", events[0]["event_type"])
            self.assertIsNone(events[0]["from_state"])
            self.assertEqual("initialized", events[0]["to_state"])

    def test_valid_transition_and_custom_event_increment_event_ids(self):
        controller = self.load_controller()
        run_id = "run-2026-06-29T020304Z-test"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            planned = self.transition(controller, target, run_id, "planned")
            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "event",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--event-type",
                    "checkpoint",
                    "--message",
                    "Subagent checkpoint.",
                    "--data-json",
                    '{"worker":"agent-a"}',
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            event = json.loads(stdout)
            self.assertEqual("planned", planned["state"]["current"])
            self.assertEqual("evt-0003", event["event_id"])
            self.assertEqual("checkpoint", event["event_type"])
            self.assertEqual({"worker": "agent-a"}, event["data"])
            events_path = target / "runs" / run_id / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["evt-0001", "evt-0002", "evt-0003"], [entry["event_id"] for entry in events])
            self.assertEqual(["initialized", "planned"], [entry["to_state"] for entry in events[:2]])
            self.assertIsNone(events[2]["to_state"])

    def test_invalid_event_type_is_rejected_but_custom_namespace_is_allowed(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-event-vocab"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "event",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--event-type",
                    "unreviewed_event",
                    "--message",
                    "This event type is not in the shared vocabulary.",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("EVENT_TYPE_INVALID", json.loads(stderr)["error_code"])

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "event",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--event-type",
                    "custom.operator.note",
                    "--message",
                    "Namespaced operator note.",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)
            event = json.loads(stdout)
            self.assertEqual("custom.operator.note", event["event_type"])

    def test_heartbeat_updates_liveness_and_appends_audit_event(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-heartbeat"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "heartbeat",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            self.assertEqual("agent-pm", run_state["agent_id"])
            self.assertIsNotNone(run_state["last_heartbeat_at"])
            self.assertEqual(run_state["last_heartbeat_at"], run_state["updated_at"])
            events = self.events(target, run_id)
            self.assertEqual("evt-0002", events[-1]["event_id"])
            self.assertEqual("heartbeat", events[-1]["event_type"])
            self.assertEqual({"state": "initialized"}, events[-1]["data"])

    def test_event_liveness_orders_dst_offsets_by_utc_instant(self):
        controller = self.load_controller()
        run_id = "run-2026-11-01T000000Z-dst-order"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            event_path = controller.events_path(target, run_id)
            controller.append_event(
                event_path,
                {
                    "event_id": "evt-dst-first",
                    "event_type": "operator.note",
                    "occurred_at": "2026-11-01T01:45:00-04:00",
                    "agent_id": "agent-pm",
                    "data": {},
                },
            )
            controller.append_event(
                event_path,
                {
                    "event_id": "evt-dst-second",
                    "event_type": "operator.note",
                    "occurred_at": "2026-11-01T01:15:00-05:00",
                    "agent_id": "agent-pm",
                    "data": {},
                },
            )

            self.assertEqual("2026-11-01T06:15:00Z", controller.latest_event_at(target, run_id))
            self.assertEqual(
                controller.parse_timestamp("2026-11-01T01:45:00-04:00"),
                controller.parse_timestamp("2026-11-01T05:45:00Z"),
            )

    def test_adopt_requires_stale_run_and_preserves_previous_owner(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-adopt"
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "adopt",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-new",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("RUN_ADOPT_THRESHOLD_REQUIRED", json.loads(stderr)["error_code"])

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "adopt",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-new",
                    "--if-stale-hours",
                    "4",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("RUN_NOT_STALE", json.loads(stderr)["error_code"])

            self.rewrite_liveness_timestamps(target, run_id, stale_at)
            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "adopt",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-new",
                    "--if-stale-hours",
                    "4",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            self.assertEqual("agent-new", run_state["agent_id"])
            self.assertIsNotNone(run_state["last_heartbeat_at"])
            recovery = run_state["recovery_history"][-1]
            self.assertEqual("adopt", recovery["action"])
            self.assertEqual("agent-pm", recovery["previous_agent_id"])
            self.assertEqual("agent-new", recovery["agent_id"])
            self.assertEqual(4.0, recovery["if_stale_hours"])
            self.assertEqual(stale_at, recovery["stale_since"])
            self.assertEqual("run_adopted", self.events(target, run_id)[-1]["event_type"])

    def test_abandon_marks_stale_run_failed_with_machine_reason(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-abandon"
        stale_at = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            self.rewrite_liveness_timestamps(target, run_id, stale_at)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "abandon",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-supervisor",
                    "--if-stale-hours",
                    "4",
                    "--reason",
                    "No owner heartbeat; abandoned for recovery.",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            self.assertEqual("failed", run_state["state"]["current"])
            self.assertEqual("failed", run_state["final_verdict"])
            failure = run_state["failure_records"][-1]
            self.assertEqual("stale_run_abandoned", failure["failure_code"])
            self.assertEqual("stale_run_abandoned", failure["machine_reason"])
            self.assertIn("No owner heartbeat", failure["reason"])
            recovery = run_state["recovery_history"][-1]
            self.assertEqual("abandon", recovery["action"])
            self.assertEqual("agent-pm", recovery["previous_agent_id"])
            self.assertEqual("run_abandoned", self.events(target, run_id)[-1]["event_type"])

    def test_concurrent_run_state_updates_are_not_lost(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-concurrent"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(RUN_CONTROLLER_PATH),
                        "--project-root",
                        str(target),
                        "event",
                        "--run-id",
                        run_id,
                        "--agent-id",
                        f"agent-{index}",
                        "--event-type",
                        "checkpoint",
                        "--message",
                        f"Concurrent checkpoint {index}.",
                        "--format",
                        "json",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(5)
            ]

            results = [process.communicate(timeout=10) for process in processes]
            self.assertEqual([0] * 5, [process.returncode for process in processes], results)
            payloads = [json.loads(stdout) for stdout, _ in results]
            self.assertEqual(5, len({payload["event_id"] for payload in payloads}))

            events_path = target / "runs" / run_id / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(6, len(events))
            self.assertEqual([f"evt-{index:04d}" for index in range(1, 7)], [event["event_id"] for event in events])
            self.assertEqual(
                {f"Concurrent checkpoint {index}." for index in range(5)},
                {event["message"] for event in events[1:]},
            )

    def test_transition_snapshots_expanded_budget_counters(self):
        controller = self.load_controller()
        run_id = "run-2026-06-29T020304Z-budget"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--to-state",
                    "planned",
                    "--questions-processed-this-run",
                    "1",
                    "--source-requests-opened-this-run",
                    "2",
                    "--releases-this-run",
                    "3",
                    "--discovery-results-this-run",
                    "4",
                    "--acquisition-downloads-this-run",
                    "5",
                    "--github-archive-bytes-this-run",
                    "6",
                    "--academic-provider-requests-this-run",
                    "7",
                    "--web-downloads-this-run",
                    "9",
                    "--manual-url-deliveries-this-run",
                    "8",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            budget_state = run_state["budget_state"]
            self.assertEqual("artifact_derived", budget_state["counter_source"])
            self.assertEqual(0, budget_state["questions_processed_this_run"])
            self.assertEqual(0, budget_state["source_requests_opened_this_run"])
            self.assertEqual(0, budget_state["releases_this_run"])
            self.assertEqual(0, budget_state["discovery_results_this_run"])
            self.assertEqual(0, budget_state["acquisition_downloads_this_run"])
            self.assertEqual(0, budget_state["github_archive_bytes_this_run"])
            self.assertEqual(0, budget_state["academic_provider_requests_this_run"])
            self.assertEqual(0, budget_state["web_downloads_this_run"])
            self.assertEqual(0, budget_state["manual_url_deliveries_this_run"])
            self.assertEqual(1, budget_state["runner_reported"]["questions_processed_this_run"])
            self.assertIn(
                {"counter": "questions_processed_this_run", "runner_reported": 1, "artifact_derived": 0},
                budget_state["counter_divergence"],
            )
            self.assertEqual([], budget_state["stop_reasons"])
            self.assertFalse(budget_state["should_stop"])

    def test_manual_url_and_web_budget_requires_explicit_override(self):
        controller = self.load_controller()
        run_id = "run-2026-07-04T010203Z-manual-url"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--to-state",
                    "planned",
                    "--manual-url-deliveries-this-run",
                    "11",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("BUDGET_EXCEEDED", json.loads(stderr)["error_code"])

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "override-manual-url-budget",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--new-limit",
                    "18",
                    "--override-reason",
                    "Supervisor approved additional official URL captures.",
                    "--approved-by",
                    "supervisor",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)
            override_state = json.loads(stdout)["budget_overrides"]["manual_url_deliveries"]
            self.assertEqual(10, override_state["previous_limit"])
            self.assertEqual(18, override_state["new_limit"])
            self.assertEqual("supervisor", override_state["approved_by"])

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--to-state",
                    "planned",
                    "--manual-url-deliveries-this-run",
                    "11",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            self.assertEqual(
                18,
                run_state["budget_state"]["manual_url_deliveries_override"]["new_limit"],
            )

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--to-state",
                    "discovering",
                    "--web-downloads-this-run",
                    "18",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)
            run_state = json.loads(stdout)
            self.assertEqual(
                18,
                run_state["budget_state"]["web_downloads_override"]["new_limit"],
            )

    def test_invalid_transition_answering_back_to_candidates_ready_is_json_error(self):
        controller = self.load_controller()
        run_id = "run-2026-06-29T030405Z-test"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            for state in ANSWERING_PATH:
                self.transition(controller, target, run_id, state)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "transition",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--to-state",
                    "candidates_ready",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("RUN_TRANSITION_INVALID", envelope["error_code"])
            self.assertEqual(
                {"run_id": run_id, "from_state": "answering", "to_state": "candidates_ready"},
                envelope["details"],
            )

    def test_finish_rejects_initialized_to_complete_and_requires_final_verdict(self):
        controller = self.load_controller()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_id = "run-2026-06-29T040506Z-test"
            self.start_run(controller, target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "finish",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("FINAL_VERDICT_REQUIRED", json.loads(stderr)["error_code"])

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "finish",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--final-verdict",
                    "complete",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("RUN_TRANSITION_INVALID", envelope["error_code"])
            self.assertEqual(
                {"run_id": run_id, "from_state": "initialized", "to_state": "complete"},
                envelope["details"],
            )

    def test_finish_complete_refuses_fresh_open_high_priority_question_without_mutation(self):
        controller = self.load_controller()
        run_id = "run-2026-07-11T010203Z-readiness-bypass"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "publication-blocker.md"
            question.write_text(
                "---\n"
                "type: question\n"
                "created: '2026-07-11'\n"
                "updated: '2026-07-11'\n"
                "status: open\n"
                "priority: high\n"
                "question: Is the publication evidence complete?\n"
                "source_ids: []\n"
                "---\n\n"
                "This required publication question remains open.\n",
                encoding="utf-8",
            )
            self.start_run(controller, target, run_id)
            for state in (*ANSWERING_PATH, "verifying"):
                self.transition(controller, target, run_id, state)
            state_path = target / "runs" / run_id / "run-state.json"
            before_state = state_path.read_bytes()
            before_events = self.events(target, run_id)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "finish",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--final-verdict",
                    "complete",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            error = json.loads(stderr)
            self.assertEqual("RUN_COMPLETION_NOT_READY", error["error_code"])
            self.assertNotEqual("ship", error["details"]["publication_verdict"])
            self.assertTrue(error["details"]["blocking_findings"])
            self.assertEqual(before_state, state_path.read_bytes())
            self.assertEqual(before_events, self.events(target, run_id))

    def test_finish_complete_records_fresh_ship_readiness_and_duplicate_is_refused(self):
        controller = self.load_controller()
        run_id = "run-2026-07-11T020304Z-ready"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            for state in (*ANSWERING_PATH, "verifying"):
                self.transition(controller, target, run_id, state)
            real_loader = controller.load_sibling_module
            readiness = SimpleNamespace(
                build_readiness_document=lambda _root: {
                    "verdict": "ship",
                    "generated_at": "2026-07-11T02:03:04Z",
                    "verdict_reasons": [],
                }
            )

            with mock.patch.object(
                controller,
                "load_sibling_module",
                side_effect=lambda stem: readiness if stem == "publication_readiness" else real_loader(stem),
            ):
                code, stdout, stderr = self.run_module(
                    controller,
                    [
                        "--project-root",
                        str(target),
                        "finish",
                        "--run-id",
                        run_id,
                        "--agent-id",
                        "agent-pm",
                        "--final-verdict",
                        "complete",
                        "--format",
                        "json",
                    ],
                )
                duplicate_code, duplicate_stdout, duplicate_stderr = self.run_module(
                    controller,
                    [
                        "--project-root",
                        str(target),
                        "finish",
                        "--run-id",
                        run_id,
                        "--agent-id",
                        "agent-pm",
                        "--final-verdict",
                        "complete",
                        "--format",
                        "json",
                    ],
                )

            self.assertEqual(0, code, stderr)
            self.assertEqual("complete", json.loads(stdout)["state"]["current"])
            completion = self.events(target, run_id)[-1]["data"]["completion_readiness"]
            self.assertEqual("ship", completion["verdict"])
            self.assertEqual("2026-07-11T02:03:04Z", completion["generated_at"])
            self.assertEqual(2, duplicate_code)
            self.assertEqual("", duplicate_stdout)
            self.assertEqual("RUN_TERMINAL", json.loads(duplicate_stderr)["error_code"])

    def test_finish_no_ship_remains_available_without_ship_readiness(self):
        controller = self.load_controller()
        run_id = "run-2026-07-11T030405Z-no-ship"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            for state in ANSWERING_PATH:
                self.transition(controller, target, run_id, state)

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "finish",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--final-verdict",
                    "no_ship",
                    "--reason",
                    "Required publication evidence remains unresolved.",
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(0, code, stderr)
            document = json.loads(stdout)
            self.assertEqual("no_ship", document["state"]["current"])
            self.assertEqual("no_ship", document["final_verdict"])

    def test_interrupted_event_append_requires_explicit_idempotent_recovery(self):
        controller = self.load_controller()
        run_id = "run-2026-07-11T040506Z-recovery"
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.start_run(controller, target, run_id)
            raw = target / "sources" / "raw" / "recovery-evidence.txt"
            user = target / "wiki" / "recovery-user-edit.md"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_text("raw evidence must survive\n", encoding="utf-8")
            user.write_text("user edit must survive\n", encoding="utf-8")
            protected = (raw.read_bytes(), user.read_bytes())
            original_replace = controller.Path.replace
            injected = False

            def fail_event_replace(path, destination):
                nonlocal injected
                if not injected and path.name.startswith(".events.jsonl.") and path.name.endswith(".tmp"):
                    injected = True
                    raise OSError(errno.ENOSPC, "injected quota exhaustion")
                return original_replace(path, destination)

            with mock.patch.object(controller.Path, "replace", new=fail_event_replace):
                code, stdout, stderr = self.run_module(
                    controller,
                    [
                        "--project-root",
                        str(target),
                        "transition",
                        "--run-id",
                        run_id,
                        "--agent-id",
                        "agent-pm",
                        "--to-state",
                        "planned",
                        "--format",
                        "json",
                    ],
                )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("RUN_MUTATION_WRITE_FAILED", envelope["error_code"])
            self.assertTrue(envelope["recoverable"])
            self.assertEqual(errno.ENOSPC, envelope["details"]["errno"])
            self.assertIn("recover", envelope["remediation"])
            pending_state = json.loads((target / "runs" / run_id / "run-state.json").read_text())
            self.assertEqual("planned", pending_state["state"]["current"])
            self.assertEqual("evt-0002", pending_state["_pending_event"]["event_id"])
            self.assertEqual(["evt-0001"], [event["event_id"] for event in self.events(target, run_id)])

            status_code, _, status_stderr = self.run_module(
                controller,
                ["--project-root", str(target), "status", "--run-id", run_id, "--format", "json"],
            )
            self.assertEqual(2, status_code)
            self.assertEqual("RUN_MUTATION_RECOVERY_REQUIRED", json.loads(status_stderr)["error_code"])

            recover_argv = [
                "--project-root",
                str(target),
                "recover",
                "--run-id",
                run_id,
                "--agent-id",
                "recovery-agent",
                "--format",
                "json",
            ]
            recover_code, recover_stdout, recover_stderr = self.run_module(controller, recover_argv)
            recovered_once = self.events(target, run_id)
            second_code, _, second_stderr = self.run_module(controller, recover_argv)

            self.assertEqual(0, recover_code, recover_stderr)
            recovered = json.loads(recover_stdout)
            self.assertNotIn("_pending_event", recovered)
            self.assertEqual(["evt-0001", "evt-0002", "evt-0003"], [event["event_id"] for event in recovered_once])
            self.assertEqual("mutation_recovered", recovered_once[-1]["event_type"])
            self.assertFalse(recovered_once[-1]["data"]["ownership_changed"])
            self.assertEqual(0, second_code, second_stderr)
            self.assertEqual(recovered_once, self.events(target, run_id))
            self.assertEqual(protected[0], raw.read_bytes())
            self.assertEqual(protected[1], user.read_bytes())

    def test_malformed_run_state_is_reported_as_json_error(self):
        controller = self.load_controller()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_id = "run-2026-06-29T050607Z-test"
            self.start_run(controller, target, run_id)
            (target / "runs" / run_id / "run-state.json").write_text("{not valid json", encoding="utf-8")

            code, stdout, stderr = self.run_module(
                controller,
                [
                    "--project-root",
                    str(target),
                    "status",
                    "--run-id",
                    run_id,
                    "--format",
                    "json",
                ],
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("RUN_STATE_INVALID", json.loads(stderr)["error_code"])

    def test_run_id_validation_rejects_path_like_and_windows_invalid_values(self):
        controller = self.load_controller()
        bad_run_ids = ("bad/child", r"bad\child", "..", "bad:child")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            for run_id in bad_run_ids:
                with self.subTest(run_id=run_id):
                    code, stdout, stderr = self.run_module(
                        controller,
                        [
                            "--project-root",
                            str(target),
                            "start",
                            "--run-id",
                            run_id,
                            "--agent-id",
                            "agent-pm",
                            "--format",
                            "json",
                        ],
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    self.assertEqual("RUN_ID_INVALID", json.loads(stderr)["error_code"])
            self.assertFalse((target / "runs" / "bad").exists())
            runs_root = target / "runs"
            run_state_files = list(runs_root.rglob("run-state.json")) if runs_root.exists() else []
            self.assertEqual([], run_state_files)
            self.assertFalse((target / "run-state.json").exists())


if __name__ == "__main__":
    unittest.main()
