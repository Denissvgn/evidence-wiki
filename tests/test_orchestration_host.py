import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli, orchestration


def work_order(action_id: str = "action-0001") -> dict:
    return {
        "schema_version": "1.0",
        "artifact_type": "orchestration_work_order",
        "orchestration_id": "orch-1",
        "action_id": action_id,
        "issued_at": "2026-07-20T00:00:00Z",
        "phase": "research",
        "skill": "research-run",
        "run_id": "run-1",
        "agent_id": "agent-1",
        "scope": {"question_slugs": ["question-1"], "request_ids": [], "candidate_ids": []},
        "provider_policy": {
            "discovery": {"enabled": True, "providers": ["arxiv"]},
            "acquisition": {"enabled": True, "providers": ["arxiv"]},
        },
        "budgets": {"action_timeout_seconds": 60},
        "inputs": ["wiki/questions/question-1.md"],
        "required_postconditions": [
            {
                "check": "question_status",
                "expected": "terminal outcome",
                "path": "wiki/questions/question-1.md",
            }
        ],
        "lease": {"duration_seconds": 60, "expires_at": "2026-07-20T00:01:00Z", "attempt": 1},
    }


def result(action_id: str = "action-0001") -> dict:
    return {
        "schema_version": "1.0",
        "action_id": action_id,
        "outcome": "completed",
        "summary": "Completed the bounded action.",
        "artifacts": ["wiki/questions/question-1.md"],
    }


def control_workspace(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "skills").mkdir()
    orchestration_root = root / "runs" / "orchestrations" / "orch-1"
    (orchestration_root / "work-orders").mkdir(parents=True)
    (orchestration_root / "work-results").mkdir()
    (root / "research.yml").write_text("project: {name: test}\n", encoding="utf-8")
    (root / "workspace-system.yml").write_text(
        "workspace_system:\n"
        '  starter_version: "0.5.0"\n'
        '  schema_version: "0.1"\n'
        '  created: "2026-05-10"\n'
        '  compatible_research_yml_contract: "0.1"\n',
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text("# Trusted agent instructions\n", encoding="utf-8")
    (root / "scripts" / "trusted.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "skills" / "research-run.md").write_text("# Trusted skill\n", encoding="utf-8")
    (orchestration_root / "session.json").write_text('{"status":"active"}\n', encoding="utf-8")
    (orchestration_root / "work-orders" / "action-0001.json").write_text(
        json.dumps(work_order()) + "\n", encoding="utf-8"
    )
    return orchestration_root


def file_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class OrchestrationHostTests(unittest.TestCase):
    def test_contract_publishes_all_orchestration_schema_versions(self):
        payload = cli._contract_payload()
        schemas = payload["artifact_schemas"]
        self.assertEqual("1.0", schemas["orchestration_session"])
        self.assertEqual("1.0", schemas["orchestration_work_order"])
        self.assertEqual("1.0", schemas["orchestration_result"])
        providers = payload["source_providers"]
        self.assertEqual(["arxiv", "openalex", "github", "web"], providers["acquisition"])
        self.assertIn("arxiv", providers["discovery"])
        self.assertIn("standards:nist", providers["discovery"])
        self.assertEqual(["legal", "authors", "companions"], providers["legacy_discovery_strategy_aliases"])

    def test_deployed_controller_issues_a_host_valid_work_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "workspace with spaces"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "deploy",
                            "--target",
                            str(root),
                            "--project-name",
                            "orchestration-host-integration",
                            "--project-description",
                            "Validate the package host against the deployed controller.",
                        ]
                    ),
                )
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "orchestrate",
                            "start",
                            "--target",
                            str(root),
                            "--orchestration-id",
                            "host-integration",
                            "--agent-id",
                            "host-test",
                            "--format",
                            "json",
                        ]
                    ),
                )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(
                    [
                        "orchestrate",
                        "next",
                        "--target",
                        str(root),
                        "--orchestration-id",
                        "host-integration",
                        "--agent-id",
                        "host-test",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(0, code)
        order = orchestration._validate_work_order(json.loads(output.getvalue()))
        self.assertEqual("action-0001", order["action_id"])
        self.assertIn(order["phase"], orchestration.WORK_ORDER_PHASES)
        self.assertTrue(all(isinstance(item, dict) for item in order["required_postconditions"]))

    def test_managed_run_completes_through_real_controller_with_fake_codex(self):
        def fake_execute(argv, **_kwargs):
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            document = result()
            document["artifacts"] = []
            output_path.write_text(json.dumps(document), encoding="utf-8")
            return orchestration.ProcessResult(0, "", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "managed workspace"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "deploy",
                            "--target",
                            str(root),
                            "--project-name",
                            "managed-host-integration",
                            "--project-description",
                            "Exercise the managed loop without a live model.",
                        ]
                    ),
                )
            stdout = io.StringIO()
            with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
                orchestration, "_execute_bounded", side_effect=fake_execute
            ), contextlib.redirect_stdout(stdout):
                code = cli.main(
                    [
                        "orchestrate",
                        "run",
                        "--target",
                        str(root),
                        "--runner",
                        "codex",
                        "--agent-id",
                        "host-test",
                        "--format",
                        "json",
                    ]
                )

            session = json.loads(stdout.getvalue())
            answers_path = root / "runs" / "orchestrations" / session["orchestration_id"] / "answers.json"
            answers_exists = answers_path.is_file()

        self.assertEqual(0, code)
        self.assertEqual("complete", session["status"])
        self.assertEqual("complete", session["verdict"])
        self.assertTrue(answers_exists)

    def test_runner_argv_is_structured_and_contains_no_bypass_flags(self):
        root = Path("/tmp/workspace with spaces")
        codex = orchestration._codex_argv(
            "/tmp/fake tools/codex",
            root,
            Path("/tmp/schema.json"),
            Path("/tmp/result.json"),
            "gpt-test",
            allow_network=True,
        )
        claude = orchestration._claude_argv("/tmp/fake tools/claude", "claude-test")

        self.assertEqual("/tmp/fake tools/codex", codex[0])
        self.assertIn(str(root), codex)
        self.assertIn("workspace-write", codex)
        self.assertIn("never", codex)
        self.assertIn("mcp_servers={}", codex)
        self.assertIn("sandbox_workspace_write.network_access=true", codex)
        self.assertIn("gpt-test", codex)
        self.assertEqual("/tmp/fake tools/claude", claude[0])
        self.assertIn("auto", claude)
        self.assertIn("WebFetch,WebSearch", claude)
        self.assertIn("--strict-mcp-config", claude)
        self.assertEqual("", claude[claude.index("--setting-sources") + 1])
        self.assertIn("claude-test", claude)
        for argv in (codex, claude):
            joined = " ".join(argv)
            self.assertNotIn("dangerously", joined)
            self.assertNotIn("bypassPermissions", joined)

    def test_network_access_requires_the_matching_network_phase(self):
        order = work_order()
        order["provider_policy"] = {
            "discovery": {"enabled": True, "providers": ["arxiv"]},
            "acquisition": {"enabled": True, "providers": ["openalex"]},
        }

        order["phase"] = "research"
        order["skill"] = "research-run"
        self.assertFalse(orchestration._work_order_allows_network(order))

        order["phase"] = "discovery"
        order["skill"] = "research-discover"
        self.assertTrue(orchestration._work_order_allows_network(order))

        order["phase"] = "candidate_review"
        self.assertFalse(orchestration._work_order_allows_network(order))

        order["phase"] = "acquisition"
        order["skill"] = "research-acquire"
        self.assertTrue(orchestration._work_order_allows_network(order))

        order["provider_policy"]["acquisition"] = {"enabled": True, "providers": []}
        self.assertFalse(orchestration._work_order_allows_network(order))

    def test_bounded_execution_truncates_without_shell(self):
        completed = orchestration._execute_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * (4 * 1024 * 1024))"],
            cwd=REPO_ROOT,
            stdin_text="",
            timeout_seconds=10,
            capture_limit=1024,
        )

        self.assertEqual(0, completed.returncode)
        self.assertTrue(completed.stdout_truncated)
        self.assertTrue(completed.stdout.startswith("x" * 1024))
        self.assertLessEqual(len(completed.stdout), 1100)
        self.assertIn("<output truncated>", completed.stdout)

    @unittest.skipUnless(os.name == "posix", "process-group liveness assertion is POSIX-specific")
    def test_timeout_terminates_spawned_runner_descendants(self):
        child_program = "import time; time.sleep(60)"
        parent_program = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable, '-c', {child_program!r}]); "
            "print(p.pid, flush=True); time.sleep(60)"
        )
        completed = orchestration._execute_bounded(
            [sys.executable, "-c", parent_program],
            cwd=REPO_ROOT,
            stdin_text="",
            timeout_seconds=1,
            capture_limit=1024,
        )

        self.assertTrue(completed.timed_out)
        child_pid = int(completed.stdout.splitlines()[0])

        def child_is_running() -> bool:
            proc_status = Path(f"/proc/{child_pid}/stat")
            if proc_status.is_file():
                try:
                    return proc_status.read_text(encoding="utf-8").split()[2] != "Z"
                except (OSError, IndexError):
                    return False
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                return False
            return True

        deadline = time.monotonic() + 3
        while child_is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(child_is_running(), "runner descendant survived the action timeout")

    def test_controller_subprocess_uses_fixed_interpreter_argv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "workspace with spaces"
            (root / "scripts").mkdir(parents=True)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            (root / "scripts" / "orchestration_controller.py").write_text("# fixture\n", encoding="utf-8")
            completed = subprocess.CompletedProcess([], 0, stdout="{}\n", stderr="")
            with mock.patch.object(orchestration.subprocess, "run", return_value=completed) as run:
                orchestration._invoke_controller(root, "status", ["--format", "json"])

        argv = run.call_args.args[0]
        self.assertEqual(sys.executable, argv[0])
        self.assertEqual(str(root / "scripts" / "orchestration_controller.py"), argv[1])
        self.assertEqual(str(root), run.call_args.kwargs["cwd"])
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_controller_json_preserves_machine_readable_paused_and_terminal_sessions(self):
        for status, code in (("paused", 4), ("blocked_on_sources", 3), ("no_ship", 2)):
            payload = {
                "schema_version": "1.0",
                "artifact_type": "orchestration_session",
                "orchestration_id": "orch-1",
                "status": status,
            }
            completed = subprocess.CompletedProcess([], code, stdout=json.dumps(payload), stderr="")
            with self.subTest(status=status), mock.patch.object(
                orchestration, "_invoke_controller", return_value=completed
            ):
                self.assertEqual(payload, orchestration._controller_json(REPO_ROOT, "status", []))

    def test_result_validation_rejects_absolute_and_parent_paths(self):
        for unsafe in ("/tmp/result.md", "../result.md", "C:\\temp\\result.md"):
            document = result()
            document["artifacts"] = [unsafe]
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(orchestration.OrchestrationHostError):
                    orchestration._validate_result(document, "action-0001")

    def test_result_validation_rejects_host_owned_orchestration_artifacts(self):
        for unsafe in (
            "runs/orchestrations/orch-1/session.json",
            "./runs/orchestrations/orch-1/work-results/action-0001.json",
            "runs\\orchestrations\\orch-1\\events.jsonl",
            "RUNS/ORCHESTRATIONS/orch-1/session.json",
        ):
            document = result()
            document["artifacts"] = [unsafe]
            with self.subTest(unsafe=unsafe), self.assertRaisesRegex(
                orchestration.OrchestrationHostError, "host-owned"
            ):
                orchestration._validate_result(document, "action-0001")

    def test_result_validation_rejects_environment_credentials(self):
        document = result()
        document["summary"] = "accidentally copied secret-value-123"
        with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret-value-123"}, clear=False):
            with self.assertRaises(orchestration.OrchestrationHostError):
                orchestration._validate_result(document, "action-0001")

    def test_runner_diagnostics_redact_url_encoded_environment_credentials(self):
        with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret/value+123"}, clear=False):
            diagnostic = "https://api.openalex.org/works?api_key=secret%2Fvalue%2B123"
            redacted = orchestration._redact(diagnostic)
        self.assertNotIn("secret/value+123", redacted)
        self.assertNotIn("secret%2Fvalue%2B123", redacted)
        self.assertIn("<redacted>", redacted)

    def test_codex_result_is_read_from_schema_constrained_output_file(self):
        observed = {}

        def fake_execute(argv, **kwargs):
            observed["argv"] = argv
            observed["prompt"] = kwargs["stdin_text"]
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(result()), encoding="utf-8")
            return orchestration.ProcessResult(0, "", "")

        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
            orchestration, "_execute_bounded", side_effect=fake_execute
        ):
            document = orchestration.execute_work_order(
                REPO_ROOT,
                work_order(),
                runner="codex",
                model="gpt-test",
                timeout_seconds=60,
            )

        self.assertEqual(result(), document)
        self.assertEqual("/tmp/fake codex", observed["argv"][0])
        self.assertIn("--output-schema", observed["argv"])
        self.assertIn('"action_id": "action-0001"', observed["prompt"])
        self.assertIn("blocked_on_sources after creating structured source requests is completed", observed["prompt"])
        self.assertIn("blocked: the work order itself cannot make progress", observed["prompt"])
        self.assertNotIn(str(REPO_ROOT), observed["prompt"])

    def test_claude_structured_output_is_validated(self):
        output = json.dumps({"type": "result", "structured_output": result()})
        completed = orchestration.ProcessResult(0, output, "")
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake claude"), mock.patch.object(
            orchestration, "_execute_bounded", return_value=completed
        ) as execute:
            document = orchestration.execute_work_order(
                REPO_ROOT,
                work_order(),
                runner="claude",
                model=None,
                timeout_seconds=60,
            )

        self.assertEqual(result(), document)
        argv = execute.call_args.args[0]
        self.assertEqual("/tmp/fake claude", argv[0])
        self.assertIn("--json-schema", argv)

    def test_runner_failure_does_not_create_a_submitted_result(self):
        completed = orchestration.ProcessResult(9, "", "runner failed")
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
            orchestration, "_execute_bounded", return_value=completed
        ):
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "remains resumable"):
                orchestration.execute_work_order(
                    REPO_ROOT,
                    work_order(),
                    runner="codex",
                    model=None,
                    timeout_seconds=60,
                )

    def test_runner_timeout_leaves_action_resumable(self):
        completed = orchestration.ProcessResult(-15, "", "", timed_out=True)
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
            orchestration, "_execute_bounded", return_value=completed
        ):
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "timed out.*remains resumable"):
                orchestration.execute_work_order(
                    REPO_ROOT,
                    work_order(),
                    runner="codex",
                    model=None,
                    timeout_seconds=60,
                )

    def test_malformed_claude_output_is_refused(self):
        completed = orchestration.ProcessResult(0, "not-json", "")
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake claude"), mock.patch.object(
            orchestration, "_execute_bounded", return_value=completed
        ):
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "invalid JSON"):
                orchestration.execute_work_order(
                    REPO_ROOT,
                    work_order(),
                    runner="claude",
                    model=None,
                    timeout_seconds=60,
                )

    def test_public_submit_requires_and_forwards_action_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            with mock.patch.object(orchestration, "_passthrough_controller", return_value=0) as passthrough:
                code = orchestration.main(
                    [
                        "submit",
                        "--target",
                        str(root),
                        "--orchestration-id",
                        "orch-1",
                        "--action-id",
                        "action-0001",
                        "--result-file",
                        "result.json",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(0, code)
        self.assertEqual("submit", passthrough.call_args.args[1])
        forwarded = passthrough.call_args.args[2]
        self.assertEqual("action-0001", forwarded[forwarded.index("--action-id") + 1])

    def test_missing_runner_fails_before_a_session_is_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(orchestration.shutil, "which", return_value=None), mock.patch.object(
                orchestration, "_controller_json"
            ) as controller, contextlib.redirect_stderr(stderr):
                code = orchestration.main(["run", "--target", str(root), "--runner", "codex"])

        self.assertEqual(orchestration.EXIT_RUNNER_FAILED, code)
        controller.assert_not_called()
        self.assertIn("not found on PATH", stderr.getvalue())

    def test_managed_run_prints_blocked_session_and_returns_semantic_exit(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "planning", "verdict": None}
        blocked = {
            "schema_version": "1.0",
            "artifact_type": "orchestration_session",
            "orchestration_id": "orch-1",
            "status": "blocked_on_sources",
            "phase": "blocked_on_sources",
            "verdict": "blocked_on_sources",
        }

        def controller(_root, command, _arguments):
            return active if command == "start" else blocked

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), contextlib.redirect_stdout(stdout):
                code = orchestration.main(
                    ["run", "--target", str(root), "--runner", "codex", "--format", "json"]
                )

        self.assertEqual(orchestration.EXIT_BLOCKED, code)
        self.assertEqual(blocked, json.loads(stdout.getvalue()))

    def test_managed_resume_prints_paused_session_and_returns_semantic_exit(self):
        paused = {
            "schema_version": "1.0",
            "artifact_type": "orchestration_session",
            "orchestration_id": "orch-1",
            "status": "paused",
            "phase": "paused",
            "verdict": "paused",
            "agent_id": "original-owner",
        }
        calls = []

        def controller(_root, command, arguments):
            calls.append((command, arguments))
            return paused

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            stdout = io.StringIO()
            with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake claude"), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), contextlib.redirect_stdout(stdout):
                code = orchestration.main(
                    [
                        "resume",
                        "--target",
                        str(root),
                        "--orchestration-id",
                        "orch-1",
                        "--runner",
                        "claude",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(orchestration.EXIT_PAUSED, code)
        self.assertEqual(paused, json.loads(stdout.getvalue()))
        next_calls = [arguments for command, arguments in calls if command == "next"]
        self.assertEqual(1, len(next_calls))
        self.assertIn("--resume", next_calls[0])
        self.assertEqual("original-owner", next_calls[0][next_calls[0].index("--agent-id") + 1])

    def test_resume_requests_reactivation_once_then_honors_a_later_pause(self):
        paused = {"orchestration_id": "orch-1", "status": "paused", "phase": "research", "verdict": "paused"}
        controller_calls = []

        def controller(_root, command, arguments):
            controller_calls.append((command, arguments))
            if command == "status":
                return paused
            if command == "next":
                return work_order()
            raise AssertionError(command)

        snapshot = object()
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
            orchestration, "_controller_json", side_effect=controller
        ), mock.patch.object(orchestration, "execute_work_order", return_value=result()), mock.patch.object(
            orchestration, "_submit_result", return_value=paused
        ), mock.patch.object(
            orchestration, "_capture_control_artifacts", return_value=snapshot
        ), mock.patch.object(
            orchestration, "_verify_control_artifacts_unchanged"
        ):
            final = orchestration.drive_session(
                REPO_ROOT,
                "orch-1",
                runner="codex",
                agent_id="agent-1",
                model=None,
                action_timeout_seconds=60,
                resume=True,
            )

        self.assertEqual(paused, final)
        next_calls = [arguments for command, arguments in controller_calls if command == "next"]
        self.assertEqual(1, len(next_calls))
        self.assertIn("--resume", next_calls[0])

    def test_runner_control_tampering_is_not_submitted_and_parent_state_is_restored(self):
        mutation_names = ("session", "work-order", "preseed-result", "config")
        for mutation_name in mutation_names:
            with self.subTest(mutation=mutation_name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                parent = control_workspace(root)
                before_parent = file_tree(parent)

                def mutate_then_complete(
                    *_args,
                    mutation_name=mutation_name,
                    parent=parent,
                    root=root,
                    **_kwargs,
                ):
                    if mutation_name == "session":
                        (parent / "session.json").write_text('{"status":"complete"}\n', encoding="utf-8")
                    elif mutation_name == "work-order":
                        (parent / "work-orders" / "action-0001.json").write_text("{}\n", encoding="utf-8")
                    elif mutation_name == "preseed-result":
                        (parent / "work-results" / "action-0001.json").write_text(
                            json.dumps(result()) + "\n", encoding="utf-8"
                        )
                    else:
                        (root / "research.yml").write_text("project: {name: attacker}\n", encoding="utf-8")
                    return result()

                active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}

                def controller(_root, command, _arguments, *, active=active):
                    if command == "status":
                        return active
                    if command == "next":
                        return work_order()
                    raise AssertionError(command)

                with mock.patch.object(
                    orchestration, "_runner_executable", return_value="/tmp/fake codex"
                ), mock.patch.object(
                    orchestration, "_controller_json", side_effect=controller
                ), mock.patch.object(
                    orchestration, "execute_work_order", side_effect=mutate_then_complete
                ), mock.patch.object(orchestration, "_submit_result") as submit:
                    with self.assertRaisesRegex(
                        orchestration.OrchestrationHostError, "CONTROL_ARTIFACT_TAMPERED.*remains resumable"
                    ) as raised:
                        orchestration.drive_session(
                            root,
                            "orch-1",
                            runner="codex",
                            agent_id="agent-1",
                            model=None,
                            action_timeout_seconds=60,
                        )

                self.assertEqual(orchestration.EXIT_RUNNER_FAILED, raised.exception.exit_code)
                submit.assert_not_called()
                self.assertEqual(before_parent, file_tree(parent))
                if mutation_name == "config":
                    self.assertIn("attacker", (root / "research.yml").read_text(encoding="utf-8"))

    def test_parent_restore_supports_platforms_without_no_follow_utime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)
            before_parent = file_tree(parent)
            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            (parent / "session.json").write_text('{"status":"tampered"}\n', encoding="utf-8")

            with mock.patch.object(orchestration.os, "supports_follow_symlinks", set()):
                with self.assertRaisesRegex(
                    orchestration.OrchestrationHostError,
                    "CONTROL_ARTIFACT_TAMPERED.*remains resumable",
                ):
                    orchestration._verify_control_artifacts_unchanged(root, snapshot)

            self.assertEqual(before_parent, file_tree(parent))

    def test_control_snapshot_rejects_symlinks_special_files_and_excess_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            (root / "scripts" / "linked.py").symlink_to(root / "research.yml")
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "CONTROL_ARTIFACT_UNSAFE.*symbolic"):
                orchestration._capture_control_artifacts(root, "orch-1")

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                control_workspace(root)
                os.mkfifo(root / "skills" / "special")
                with self.assertRaisesRegex(
                    orchestration.OrchestrationHostError, "CONTROL_ARTIFACT_UNSAFE.*not a regular"
                ):
                    orchestration._capture_control_artifacts(root, "orch-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            with mock.patch.object(orchestration, "MAX_CONTROL_ARTIFACT_BYTES", 8), self.assertRaisesRegex(
                orchestration.OrchestrationHostError, "CONTROL_ARTIFACT_UNSAFE.*snapshot limit"
            ):
                orchestration._capture_control_artifacts(root, "orch-1")


if __name__ == "__main__":
    unittest.main()
