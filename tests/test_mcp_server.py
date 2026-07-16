import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
MCP_SCRIPT_PATH = SCRIPTS / "serve_mcp.py"
INIT_SCRIPT_PATH = SCRIPTS / "init_research_workspace.py"
INTAKE_SCRIPT_PATH = SCRIPTS / "intake_questions.py"
EXPORT_SCRIPT_PATH = SCRIPTS / "export_answers.py"
QUESTION_STATUS_SCRIPT_PATH = SCRIPTS / "question_status.py"
QUERY_INDEX_SCRIPT_PATH = SCRIPTS / "query_index.py"
SOURCE_REQUESTS_SCRIPT_PATH = SCRIPTS / "source_requests.py"
WORKSPACE_STATUS_SCRIPT_PATH = SCRIPTS / "workspace_status.py"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MCP = load_script_module("research_mcp_server", MCP_SCRIPT_PATH)
INIT = load_script_module("research_mcp_init", INIT_SCRIPT_PATH)
INTAKE = load_script_module("research_mcp_intake", INTAKE_SCRIPT_PATH)
EXPORT = load_script_module("research_mcp_export", EXPORT_SCRIPT_PATH)
QUESTION_STATUS = load_script_module("research_mcp_question_status", QUESTION_STATUS_SCRIPT_PATH)
QUERY_INDEX = load_script_module("research_mcp_query_index", QUERY_INDEX_SCRIPT_PATH)
SOURCE_REQUESTS = load_script_module("research_mcp_source_requests", SOURCE_REQUESTS_SCRIPT_PATH)
WORKSPACE_STATUS = load_script_module("research_mcp_workspace_status", WORKSPACE_STATUS_SCRIPT_PATH)


def without_generated_at(payload: dict) -> dict:
    clone = dict(payload)
    clone.pop("generated_at", None)
    return clone


def without_jsonrpc_correlation_id(payload: dict) -> dict:
    clone = json.loads(json.dumps(payload))
    error = clone.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict):
            data.pop("correlation_id", None)
    return clone


class McpServerTests(unittest.TestCase):
    def init_workspace(
        self,
        root: Path,
        questions: list[dict] | None = None,
        handoff: dict | None = None,
        name: str = "mcp-workspace",
    ) -> Path:
        target = root / name
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = questions or [
            {"id": "benchmarks", "question": "What benchmarks matter?", "priority": "high"}
        ]
        if handoff is not None:
            profile["workspace_init"]["handoff"] = handoff
        profile_path = root / f"{name}-profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def call_tool(self, server: object, name: str, arguments: dict | None = None, request_id: int = 1) -> dict:
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        self.assertIsNotNone(response)
        self.assertNotIn("error", response)
        return response["result"]

    def assert_error_payload_has_correlation_id(self, result: dict) -> str:
        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload, json.loads(result["content"][0]["text"]))
        self.assertIn("details", payload)
        return self.assert_correlation_id(payload["details"].get("correlation_id"))

    def assert_correlation_id(self, correlation_id: object) -> str:
        if not isinstance(correlation_id, str):
            self.fail(f"correlation_id must be a string, got {type(correlation_id).__name__}")
        self.assertTrue(correlation_id)
        self.assertRegex(correlation_id, r"\A[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
        return correlation_id

    def assert_jsonrpc_error_does_not_leak(self, response: dict, *raw_fragments: str) -> None:
        client_view = json.dumps(without_jsonrpc_correlation_id(response))
        for fragment in raw_fragments:
            self.assertNotIn(fragment, client_view)

    def workspace_snapshot(self, target: Path) -> dict[str, bytes]:
        return {
            path.relative_to(target).as_posix(): path.read_bytes()
            for path in sorted(target.rglob("*"))
            if path.is_file()
        }

    def test_initialize_and_tools_list_expose_expected_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)

            initialize = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "unit-test", "version": "1.0"},
                    },
                }
            )
            self.assertEqual(MCP.MCP_PROTOCOL_VERSION, initialize["result"]["protocolVersion"])
            self.assertEqual({"tools": {"listChanged": False}}, initialize["result"]["capabilities"])
            self.assertEqual("evidence-wiki", initialize["result"]["serverInfo"]["name"])
            instructions = initialize["result"]["instructions"]
            self.assertIn("Restart this MCP process after `evidence-wiki upgrade`", instructions)
            self.assertIn("refreshed workspace tooling is loaded", instructions)

            tools_response = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}
            self.assertEqual(
                {
                    "workspace_status",
                    "question_status",
                    "query_index",
                    "intake_questions",
                    "export_answers",
                    "source_requests_list",
                },
                set(tools),
            )
            self.assertEqual(["query"], tools["query_index"]["inputSchema"]["required"])
            self.assertEqual(
                QUERY_INDEX.MAX_QUERY_LIMIT,
                tools["query_index"]["inputSchema"]["properties"]["limit"]["maximum"],
            )
            self.assertEqual(["batch"], tools["intake_questions"]["inputSchema"]["required"])

    def test_read_tools_return_same_json_payloads_as_underlying_scripts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)

            status_result = self.call_tool(server, "workspace_status")
            self.assertFalse(status_result["isError"])
            self.assertEqual(
                without_generated_at(WORKSPACE_STATUS.build_status_document(target)),
                without_generated_at(status_result["structuredContent"]),
            )

            question_result = self.call_tool(server, "question_status")
            question_status_payload = question_result["structuredContent"]
            config = QUESTION_STATUS.load_config(target)
            questions_dir = QUESTION_STATUS.questions_directory(target, config)
            report = QUESTION_STATUS.build_report(QUESTION_STATUS.collect_questions(questions_dir))
            self.assertEqual(report, {key: question_status_payload[key] for key in report})

            query_result = self.call_tool(server, "query_index", {"query": "benchmarks", "limit": 2})
            self.assertFalse(query_result["isError"])
            query_payload = query_result["structuredContent"]
            config = QUERY_INDEX.load_config(target)
            index_path = QUERY_INDEX.resolve_index_path(target, None)
            results, indexed = QUERY_INDEX.query_with_optional_fts(target, config, "all", "benchmarks", 2, index_path)
            expected_results = QUERY_INDEX.enrich_related_source_ids(
                QUERY_INDEX.add_engine(results, QUERY_INDEX.LEXICAL_ENGINE),
                QUERY_INDEX.citation_relation_graph(target, config),
            )
            self.assertEqual("benchmarks", query_payload["query"])
            self.assertEqual(indexed, query_payload["indexed_documents"])
            self.assertEqual(expected_results, query_payload["results"])

            export_result = self.call_tool(server, "export_answers")
            self.assertEqual(
                without_generated_at(EXPORT.build_export(target, None)),
                without_generated_at(export_result["structuredContent"]),
            )

            request_result = self.call_tool(server, "source_requests_list", {"status": ["open"]})
            expected_requests = SOURCE_REQUESTS.run_list(
                type("Args", (), {"project_root": str(target), "status": ["open"]})()
            )
            self.assertEqual(
                without_generated_at(expected_requests),
                without_generated_at(request_result["structuredContent"]),
            )

    def test_query_index_limit_is_capped_before_search(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)
            captured_limits: list[int] = []

            def fake_query_with_optional_fts(project_root, config, scope, query, limit, index_path):
                captured_limits.append(limit)
                return [], 0

            with mock.patch.dict(MCP._SIBLING_CACHE, {"query_index": QUERY_INDEX}, clear=False):
                with mock.patch.object(
                    QUERY_INDEX,
                    "query_with_optional_fts",
                    side_effect=fake_query_with_optional_fts,
                ):
                    result = self.call_tool(
                        server,
                        "query_index",
                        {"query": "benchmarks", "limit": QUERY_INDEX.MAX_QUERY_LIMIT + 500},
                    )

            self.assertFalse(result["isError"])
            self.assertEqual([QUERY_INDEX.MAX_QUERY_LIMIT], captured_limits)

    def test_intake_questions_dry_run_and_apply_match_script_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dry_target = self.init_workspace(root, name="dry-run-workspace")
            batch = {
                "schema_version": "1.0",
                "questions": [{"question": "Which benchmarks are robust?", "priority": "high"}],
            }

            server = MCP.ResearchWikiMcpServer(dry_target)
            dry_result = self.call_tool(server, "intake_questions", {"batch": batch, "dry_run": True})
            expected_dry = INTAKE.run_intake_document(dry_target, batch, dry_run=True, from_file_label="mcp")
            self.assertEqual(
                without_generated_at(expected_dry),
                without_generated_at(dry_result["structuredContent"]),
            )
            self.assertFalse((dry_target / "wiki" / "questions" / "which-benchmarks-are-robust.md").exists())

            apply_target = self.init_workspace(root, name="apply-workspace")
            apply_server = MCP.ResearchWikiMcpServer(apply_target)
            apply_result = self.call_tool(apply_server, "intake_questions", {"batch": batch})
            self.assertFalse(apply_result["isError"])
            self.assertEqual(1, apply_result["structuredContent"]["counts"]["created"])
            self.assertTrue((apply_target / "wiki" / "questions" / "which-benchmarks-are-robust.md").is_file())

    def test_intake_questions_limit_failure_uses_shared_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text())
            config["run"]["max_open_questions_total"] = 1
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            server = MCP.ResearchWikiMcpServer(target)

            result = self.call_tool(
                server,
                "intake_questions",
                {
                    "batch": {
                        "schema_version": "1.0",
                        "questions": [{"question": "Which benchmark suites are robust?"}],
                    }
                },
            )

            self.assertTrue(result["isError"])
            self.assertEqual("INTAKE_TOTAL_CAP_EXCEEDED", result["structuredContent"]["error_code"])
            self.assertEqual(result["structuredContent"], json.loads(result["content"][0]["text"]))
            self.assertFalse((target / "wiki" / "questions" / "which-benchmark-suites-are-robust.md").exists())

    def test_intake_questions_field_cap_failure_preserves_error_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)

            result = self.call_tool(
                server,
                "intake_questions",
                {
                    "batch": {
                        "schema_version": "1.0",
                        "questions": [{"question": "Which benchmark suites are robust?", "summary": "S" * 1025}],
                    }
                },
            )

            self.assertTrue(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual("INTAKE_FIELD_TOO_LONG", payload["error_code"])
            correlation_id = self.assert_error_payload_has_correlation_id(result)
            self.assertEqual(
                [
                    {
                        "item_index": 0,
                        "field": "summary",
                        "actual_bytes": 1025,
                        "max_bytes": 1024,
                    }
                ],
                payload["details"]["violations"],
            )
            self.assertEqual(1024, payload["details"]["max_question_bytes"])
            self.assertEqual(1024, payload["details"]["max_summary_bytes"])
            self.assertEqual(8192, payload["details"]["max_context_bytes"])
            self.assertTrue(correlation_id)
            self.assertEqual(payload, json.loads(result["content"][0]["text"]))
            self.assertFalse((target / "wiki" / "questions" / "which-benchmark-suites-are-robust.md").exists())

    def test_intake_questions_rejects_oversized_mcp_batch_before_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text())
            config["run"]["max_mcp_intake_batch_questions"] = 2
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            log_before = (target / "log.md").read_text(encoding="utf-8")
            server = MCP.ResearchWikiMcpServer(target)

            result = self.call_tool(
                server,
                "intake_questions",
                {
                    "batch": {
                        "schema_version": "1.0",
                        "questions": [
                            {"question": "Which benchmark suites are robust?"},
                            {"question": "Which datasets are robust?"},
                            {"question": "Which metrics are robust?"},
                        ],
                    }
                },
            )

            self.assertTrue(result["isError"])
            payload = result["structuredContent"]
            self.assertEqual("INTAKE_BATCH_TOO_LARGE", payload["error_code"])
            self.assertEqual(
                {
                    "submitted_questions": 3,
                    "max_mcp_intake_batch_questions": 2,
                    "config_field": "run.max_mcp_intake_batch_questions",
                },
                payload["details"],
            )
            self.assertEqual(payload, json.loads(result["content"][0]["text"]))
            self.assertEqual(log_before, (target / "log.md").read_text(encoding="utf-8"))
            for slug in (
                "which-benchmark-suites-are-robust",
                "which-datasets-are-robust",
                "which-metrics-are-robust",
            ):
                self.assertFalse((target / "wiki" / "questions" / f"{slug}.md").exists())

    def test_tools_reject_unknown_fields_before_execution_or_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)
            before = self.workspace_snapshot(target)

            unknown_argument = self.call_tool(server, "workspace_status", {"unexpected": True})
            unknown_param_response = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "workspace_status", "arguments": {}, "unexpected": True},
                }
            )
            after = self.workspace_snapshot(target)

        self.assertTrue(unknown_argument["isError"])
        self.assertEqual("VALUE_INVALID", unknown_argument["structuredContent"]["error_code"])
        self.assertEqual(["unexpected"], unknown_argument["structuredContent"]["details"]["unknown_fields"])
        self.assertNotIn("error", unknown_param_response)
        unknown_param = unknown_param_response["result"]
        self.assertTrue(unknown_param["isError"])
        self.assertEqual("VALUE_INVALID", unknown_param["structuredContent"]["error_code"])
        self.assertEqual(before, after)

    def test_read_tools_and_dry_run_are_read_only_and_intake_stays_in_declared_write_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)
            before = self.workspace_snapshot(target)
            batch = {
                "schema_version": "1.0",
                "questions": [
                    {
                        "id": "mcp-boundary-question",
                        "question": "Which boundaries may MCP intake write?",
                        "priority": "high",
                    }
                ],
            }

            for name, arguments in (
                ("workspace_status", {}),
                ("question_status", {}),
                ("query_index", {"query": "benchmarks"}),
                ("export_answers", {}),
                ("source_requests_list", {}),
                ("intake_questions", {"batch": batch, "dry_run": True}),
            ):
                result = self.call_tool(server, name, arguments)
                self.assertFalse(result["isError"], name)
            self.assertEqual(before, self.workspace_snapshot(target))

            applied = self.call_tool(server, "intake_questions", {"batch": batch})
            after = self.workspace_snapshot(target)

        self.assertFalse(applied["isError"])
        changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
        self.assertEqual(
            {
                ".locks/log.lock",
                "index.md",
                "log.md",
                "wiki/questions/mcp-boundary-question.md",
            },
            changed,
        )

    def test_tool_execution_errors_use_shared_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)

            missing_query = self.call_tool(server, "query_index", {})
            self.assertTrue(missing_query["isError"])
            self.assertEqual("QUERY_MISSING", missing_query["structuredContent"]["error_code"])
            self.assertEqual(missing_query["structuredContent"], json.loads(missing_query["content"][0]["text"]))

            missing_workspace = MCP.ResearchWikiMcpServer(Path(tmpdir) / "missing-workspace")
            missing_status = self.call_tool(missing_workspace, "workspace_status")
            self.assertTrue(missing_status["isError"])
            self.assertEqual("CONFIG_MISSING", missing_status["structuredContent"]["error_code"])

    def test_export_answers_reports_handoff_signature_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                target = self.init_workspace(
                    root,
                    handoff={
                        "task_id": "chain-task-0042",
                        "requested_by": "planner-agent",
                        "chain_run_id": "run-2026-06-09-a",
                    },
                )

            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["project"]["handoff"]["chain_run_id"] = "tampered-run"
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            server = MCP.ResearchWikiMcpServer(target)

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                result = self.call_tool(server, "export_answers")

            self.assertTrue(result["isError"])
            self.assertEqual("HANDOFF_SIGNATURE_INVALID", result["structuredContent"]["error_code"])
            self.assertEqual(
                "Handoff signature verification failed.",
                result["structuredContent"]["message"],
            )

    def test_non_string_system_exit_becomes_tool_error_content(self):
        for exit_code, _expected_message in (
            (2, "Tool exited with status 2."),
            (None, "Tool exited without a status code."),
        ):
            with self.subTest(exit_code=exit_code):
                server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())

                def exit_from_tool(name, arguments, exit_code=exit_code):
                    raise SystemExit(exit_code)

                server.call_tool_payload = exit_from_tool

                result = server.call_tool({"name": "workspace_status", "arguments": {}})

                self.assertTrue(result["isError"])
                self.assertEqual("Workspace tool failed before completing.", result["structuredContent"]["message"])
                self.assertEqual("WORKSPACE_UNREADABLE", result["structuredContent"]["error_code"])
                correlation_id = self.assert_error_payload_has_correlation_id(result)
                self.assertTrue(correlation_id)
                self.assertEqual(result["structuredContent"], json.loads(result["content"][0]["text"]))

    def test_os_error_tool_failure_is_sanitized_and_logged(self):
        secret_path = "/tmp/private-workspace/raw/secret-note.md"
        raw_message = f"Permission denied while reading {secret_path}"
        server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())

        def fail_from_tool(name, arguments):
            raise OSError(raw_message)

        server.call_tool_payload = fail_from_tool
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = server.call_tool({"name": "workspace_status", "arguments": {}})

        payload = result["structuredContent"]
        client_text = result["content"][0]["text"]
        correlation_id = self.assert_error_payload_has_correlation_id(result)
        self.assertEqual("WORKSPACE_UNREADABLE", payload["error_code"])
        self.assertEqual("Workspace tool failed before completing.", payload["message"])
        self.assertNotIn(secret_path, client_text)
        self.assertNotIn("Permission denied", client_text)
        self.assertIn(correlation_id, stderr.getvalue())
        self.assertIn(raw_message, stderr.getvalue())

    def test_string_system_exit_failure_is_sanitized_and_logged(self):
        secret_path = "/tmp/private-workspace/research.yml"
        raw_message = f"Invalid YAML in {secret_path}: while scanning a quoted scalar"
        server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())

        def fail_from_tool(name, arguments):
            raise SystemExit(raw_message)

        server.call_tool_payload = fail_from_tool
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = server.call_tool({"name": "workspace_status", "arguments": {}})

        payload = result["structuredContent"]
        client_text = result["content"][0]["text"]
        correlation_id = self.assert_error_payload_has_correlation_id(result)
        self.assertEqual("CONFIG_INVALID", payload["error_code"])
        self.assertEqual("Workspace configuration is invalid.", payload["message"])
        self.assertNotIn(secret_path, client_text)
        self.assertNotIn("while scanning", client_text)
        self.assertIn(correlation_id, stderr.getvalue())
        self.assertIn(raw_message, stderr.getvalue())

    def test_sqlite_tool_failure_is_sanitized_and_logged(self):
        secret_path = "/tmp/private-workspace/.research-cache/index.db"
        raw_message = f"unable to open database file at {secret_path}: malformed sqlite_master"
        server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())

        def fail_from_tool(name, arguments):
            raise sqlite3.Error(raw_message)

        server.call_tool_payload = fail_from_tool
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            result = server.call_tool({"name": "query_index", "arguments": {"query": "benchmarks"}})

        payload = result["structuredContent"]
        client_text = result["content"][0]["text"]
        correlation_id = self.assert_error_payload_has_correlation_id(result)
        self.assertEqual("WORKSPACE_UNREADABLE", payload["error_code"])
        self.assertEqual("Workspace tool failed before completing.", payload["message"])
        self.assertNotIn(secret_path, client_text)
        self.assertNotIn("sqlite_master", client_text)
        self.assertIn(correlation_id, stderr.getvalue())
        self.assertIn(raw_message, stderr.getvalue())

    def test_protocol_errors_and_stdio_loop_emit_only_json_rpc_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            server = MCP.ResearchWikiMcpServer(target)

            unknown_tool = server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "no_such_tool", "arguments": {}},
                }
            )
            self.assertEqual(-32602, unknown_tool["error"]["code"])

            protocol_server = MCP.ResearchWikiMcpServer(target)
            inbound = io.StringIO(
                "\n".join(
                    [
                        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    ]
                )
                + "\n"
            )
            outbound = io.StringIO()
            protocol_server.serve(inbound, outbound)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(2, len(lines))
            messages = [json.loads(line) for line in lines]
            self.assertEqual([1, 2], [message["id"] for message in messages])
            self.assertTrue(all(message["jsonrpc"] == "2.0" for message in messages))

    def test_stdio_subprocess_processes_queued_calls_exits_at_eof_and_restarts_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            command = [sys.executable, str(target / "scripts" / "serve_mcp.py"), "--target", str(target)]
            first_input = (
                "\n".join(
                    json.dumps(message)
                    for message in (
                        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "intake_questions",
                                "arguments": {
                                    "batch": {
                                        "schema_version": "1.0",
                                        "questions": [
                                            {
                                                "id": "queued-mcp-one",
                                                "question": "Does queued MCP call one persist?",
                                            }
                                        ],
                                    }
                                },
                            },
                        },
                        {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {
                                "name": "intake_questions",
                                "arguments": {
                                    "batch": {
                                        "schema_version": "1.0",
                                        "questions": [
                                            {
                                                "id": "queued-mcp-two",
                                                "question": "Does queued MCP call two persist?",
                                            }
                                        ],
                                    }
                                },
                            },
                        },
                    )
                )
                + "\n"
            )
            first = subprocess.run(
                command,
                input=first_input,
                text=True,
                capture_output=True,
                check=False,
            )
            restart = subprocess.run(
                command,
                input=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {"name": "question_status", "arguments": {}},
                    }
                )
                + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            queued_one_exists = (target / "wiki" / "questions" / "queued-mcp-one.md").is_file()
            queued_two_exists = (target / "wiki" / "questions" / "queued-mcp-two.md").is_file()

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("", first.stderr)
        first_messages = [json.loads(line) for line in first.stdout.splitlines() if line.strip()]
        self.assertEqual([1, 2, 3], [message["id"] for message in first_messages])
        self.assertTrue(all(message["jsonrpc"] == "2.0" for message in first_messages))
        self.assertTrue(all(not message.get("result", {}).get("isError", False) for message in first_messages))
        self.assertEqual(0, restart.returncode, restart.stderr)
        self.assertEqual("", restart.stderr)
        restart_lines = [line for line in restart.stdout.splitlines() if line.strip()]
        self.assertEqual(1, len(restart_lines))
        restart_message = json.loads(restart_lines[0])
        self.assertEqual(4, restart_message["id"])
        self.assertEqual(3, restart_message["result"]["structuredContent"]["total"])
        self.assertTrue(queued_one_exists)
        self.assertTrue(queued_two_exists)

    def test_mcp_relative_symlink_target_resolves_to_one_canonical_workspace_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, name="canonical Ω workspace")
            alias = root / "workspace alias"
            try:
                alias.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable on this platform: {exc}")
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                server = MCP.ResearchWikiMcpServer(alias.name)
            finally:
                os.chdir(previous_cwd)
            status = self.call_tool(server, "workspace_status")
            export = self.call_tool(server, "export_answers")
            direct_export = EXPORT.build_export(alias, None)

        self.assertEqual(target.resolve(), server.project_root)
        self.assertFalse(status["isError"])
        self.assertEqual(
            str(target.resolve()),
            status["structuredContent"]["workspace_health"]["project_root"],
        )
        self.assertFalse(export["isError"])
        self.assertEqual(
            without_generated_at(direct_export),
            without_generated_at(export["structuredContent"]),
        )
        self.assertEqual(
            "wiki/questions/benchmarks.md",
            export["structuredContent"]["questions"][0]["question_page"],
        )

    def test_json_rpc_rejects_invalid_ids_without_echoing_them(self):
        server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())
        invalid_ids = [
            {"nested": "object"},
            ["array"],
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
        ]

        for message_id in invalid_ids:
            with self.subTest(message_id=repr(message_id)):
                response = server.handle_message({"jsonrpc": "2.0", "id": message_id, "method": "tools/list"})

                self.assertEqual("2.0", response["jsonrpc"])
                self.assertIsNone(response["id"])
                self.assertEqual(MCP.ERR_INVALID_REQUEST, response["error"]["code"])
                self.assertEqual("id must be a string, finite number, or null", response["error"]["message"])

    def test_json_rpc_accepts_valid_request_ids(self):
        server = MCP.ResearchWikiMcpServer(tempfile.gettempdir())
        valid_ids = ["req-1", 0, 42, 3.25, None]

        for message_id in valid_ids:
            with self.subTest(message_id=repr(message_id)):
                response = server.handle_message({"jsonrpc": "2.0", "id": message_id, "method": "tools/list"})

                self.assertEqual("2.0", response["jsonrpc"])
                self.assertEqual(message_id, response["id"])
                self.assertIn("tools", response["result"])

    def test_no_id_request_style_messages_are_ignored_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            protocol_server = MCP.ResearchWikiMcpServer(target)
            questions_dir = target / "wiki" / "questions"
            question_paths_before = sorted(path.name for path in questions_dir.glob("*.md"))
            log_before = (target / "log.md").read_text()
            inbound = io.StringIO(
                "\n".join(
                    [
                        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                        json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}}),
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "tools/call",
                                "params": {
                                    "name": "intake_questions",
                                    "arguments": {
                                        "batch": {
                                            "schema_version": "1.0",
                                            "questions": [
                                                {
                                                    "question": "Should notification-style tool calls mutate?",
                                                    "priority": "low",
                                                }
                                            ],
                                        }
                                    },
                                },
                            }
                        ),
                        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                    ]
                )
                + "\n"
            )
            outbound = io.StringIO()

            protocol_server.serve(inbound, outbound)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(1, len(lines))
            response = json.loads(lines[0])
            self.assertEqual(2, response["id"])
            self.assertIn("tools", response["result"])
            self.assertEqual(question_paths_before, sorted(path.name for path in questions_dir.glob("*.md")))
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_stdio_loop_rejects_oversized_json_rpc_line_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            protocol_server = MCP.ResearchWikiMcpServer(target)
            valid_request = json.dumps(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                separators=(",", ":"),
            )
            inbound = io.StringIO(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"pad": "x" * 80}})
                + "\n"
                + valid_request
                + "\n"
            )
            outbound = io.StringIO()

            protocol_server.serve(inbound, outbound, max_line_bytes=64)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(2, len(lines))
            oversized_response = json.loads(lines[0])
            self.assertEqual("2.0", oversized_response["jsonrpc"])
            self.assertIsNone(oversized_response["id"])
            self.assertEqual(MCP.ERR_INVALID_REQUEST, oversized_response["error"]["code"])
            self.assertEqual("JSON-RPC message exceeds maximum size", oversized_response["error"]["message"])
            self.assertEqual({"max_line_bytes": 64}, oversized_response["error"]["data"])

            tools_response = json.loads(lines[1])
            self.assertEqual(2, tools_response["id"])
            self.assertIn("tools", tools_response["result"])

    def test_stdio_loop_sanitizes_parse_errors_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            protocol_server = MCP.ResearchWikiMcpServer(target)
            malformed_line = '{"jsonrpc":"2.0","id":1,"method":"tools/list",bad}'
            fixed_correlation_id = "123e4567e89b42d3a456426614174bad"
            inbound = io.StringIO(
                malformed_line + "\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
            )
            outbound = io.StringIO()
            stderr = io.StringIO()

            with mock.patch.object(MCP.uuid, "uuid4", return_value=mock.Mock(hex=fixed_correlation_id)):
                with contextlib.redirect_stderr(stderr):
                    protocol_server.serve(inbound, outbound)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(2, len(lines))
            parse_error = json.loads(lines[0])
            self.assertEqual("2.0", parse_error["jsonrpc"])
            self.assertIsNone(parse_error["id"])
            self.assertEqual(MCP.ERR_PARSE, parse_error["error"]["code"])
            self.assertEqual("Parse error", parse_error["error"]["message"])
            correlation_id = self.assert_correlation_id(parse_error["error"]["data"]["correlation_id"])
            self.assertEqual(fixed_correlation_id, correlation_id)
            self.assertIn("bad", correlation_id)
            self.assert_jsonrpc_error_does_not_leak(parse_error, "Expecting", "bad")
            self.assertIn(correlation_id, stderr.getvalue())
            self.assertIn("JSONDecodeError", stderr.getvalue())
            self.assertIn("Expecting", stderr.getvalue())

            leaked_parse_error = json.loads(json.dumps(parse_error))
            leaked_parse_error["error"]["data"]["raw_input"] = malformed_line
            with self.assertRaises(AssertionError):
                self.assert_jsonrpc_error_does_not_leak(leaked_parse_error, "bad")

            tools_response = json.loads(lines[1])
            self.assertEqual(2, tools_response["id"])
            self.assertIn("tools", tools_response["result"])

    def test_stdio_loop_reports_unexpected_message_exception_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            protocol_server = MCP.ResearchWikiMcpServer(target)
            original_handle_message = protocol_server.handle_message

            def flaky_handle_message(message):
                if isinstance(message, dict) and message.get("id") == 1:
                    raise RuntimeError("boom from handler")
                return original_handle_message(message)

            protocol_server.handle_message = flaky_handle_message
            inbound = io.StringIO(
                "\n".join(
                    [
                        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
                        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    ]
                )
                + "\n"
            )
            outbound = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                protocol_server.serve(inbound, outbound)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(2, len(lines))
            internal_error = json.loads(lines[0])
            self.assertEqual("2.0", internal_error["jsonrpc"])
            self.assertEqual(1, internal_error["id"])
            self.assertEqual(MCP.ERR_INTERNAL, internal_error["error"]["code"])
            self.assertEqual("Internal server error", internal_error["error"]["message"])
            correlation_id = self.assert_correlation_id(internal_error["error"]["data"]["correlation_id"])
            self.assertIn(correlation_id, stderr.getvalue())

            tools_response = json.loads(lines[1])
            self.assertEqual(2, tools_response["id"])
            self.assertIn("tools", tools_response["result"])

    def test_stdio_loop_internal_errors_do_not_echo_invalid_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            protocol_server = MCP.ResearchWikiMcpServer(target)

            def always_fails(message):
                raise RuntimeError(f"boom from handler for {message!r}")

            protocol_server.handle_message = always_fails
            inbound = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": {"bad": "id"}, "method": "tools/list"}) + "\n")
            outbound = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                protocol_server.serve(inbound, outbound)

            lines = [line for line in outbound.getvalue().splitlines() if line.strip()]
            self.assertEqual(1, len(lines))
            internal_error = json.loads(lines[0])
            self.assertEqual("2.0", internal_error["jsonrpc"])
            self.assertIsNone(internal_error["id"])
            self.assertEqual(MCP.ERR_INTERNAL, internal_error["error"]["code"])
            self.assertEqual("Internal server error", internal_error["error"]["message"])
            correlation_id = self.assert_correlation_id(internal_error["error"]["data"]["correlation_id"])
            self.assertIn(correlation_id, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
