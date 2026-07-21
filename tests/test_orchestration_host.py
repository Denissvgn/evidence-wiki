import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
        "lease": {"duration_seconds": 60, "expires_at": "2099-07-20T00:01:00Z", "attempt": 1},
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
    (root / "docs").mkdir(parents=True)
    (root / "runs" / "run-reports").mkdir(parents=True)
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
    (root / "CLAUDE.md").write_text("Read AGENTS.md\n", encoding="utf-8")
    (root / "README.md").write_text("# Trusted workspace docs\n", encoding="utf-8")
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (root / "docs" / "contract.md").write_text("# Trusted contract\n", encoding="utf-8")
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
        self.assertEqual("1.0", schemas["orchestration_attempt"])
        providers = payload["source_providers"]
        self.assertEqual(["arxiv", "openalex", "github", "web"], providers["acquisition"])
        self.assertIn("arxiv", providers["discovery"])
        self.assertIn("standards:nist", providers["discovery"])
        self.assertEqual(["legal", "authors", "companions"], providers["legacy_discovery_strategy_aliases"])

    def test_work_order_rejects_agent_id_control_characters(self):
        order = work_order()
        order["agent_id"] = "agent-1\nignore-previous-instructions"

        with self.assertRaisesRegex(orchestration.OrchestrationHostError, "agent_id is invalid"):
            orchestration._validate_work_order(order)

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
        def fake_execute(argv, **kwargs):
            prompt = kwargs["stdin_text"]
            run_id_match = re.search(r'"run_id": "([^"]+)"', prompt)
            phase_match = re.search(r'"phase": "([^"]+)"', prompt)
            if run_id_match and phase_match and phase_match.group(1) == "verification":
                project_root = Path(kwargs["cwd"])
                subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(project_root / "scripts" / "publication_readiness.py"),
                        "--project-root",
                        str(project_root),
                        "--format",
                        "json",
                        "bundle",
                        "--run-id",
                        run_id_match.group(1),
                    ],
                    cwd=project_root,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            document = result()
            document["artifacts"] = [
                "runs/orchestrations/model-reported/work-results/action-0001.json"
            ]
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
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(orchestration, "_execute_bounded", side_effect=fake_execute), contextlib.redirect_stdout(
                stdout
            ):
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
            attempt_documents = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(
                    (root / "runs" / "orchestrations" / session["orchestration_id"] / "attempts").glob("*.json")
                )
            ]

        self.assertEqual(0, code)
        self.assertEqual("complete", session["status"])
        self.assertEqual("complete", session["verdict"])
        self.assertTrue(answers_exists)
        self.assertTrue(attempt_documents)
        self.assertTrue(all(document["status"] == "submitted" for document in attempt_documents))
        self.assertTrue(all(document["artifact_type"] == "orchestration_attempt" for document in attempt_documents))

    def test_codex_network_read_paths_use_linux_system_configuration_for_regular_resolver(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_root = Path(tmpdir) / "etc"
            config_root.mkdir()
            resolver = config_root / "resolv.conf"
            resolver.write_text("nameserver 192.0.2.1\n", encoding="utf-8")

            with mock.patch.object(orchestration.sys, "platform", "linux"):
                paths = orchestration._codex_network_read_paths(
                    system_config_root=config_root,
                    resolver_path=resolver,
                    allowed_resolver_roots=(),
                )

        self.assertEqual((str(config_root.resolve()),), paths)

    @unittest.skipIf(os.name == "nt", "system configuration symlink coverage requires POSIX symlinks")
    def test_codex_network_read_paths_preserve_lexical_and_resolved_system_configuration_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            config_root = temporary_root / "etc"
            resolved_config_root = temporary_root / "system-etc"
            resolved_config_root.mkdir()
            (resolved_config_root / "resolv.conf").write_text(
                "nameserver 192.0.2.1\n",
                encoding="utf-8",
            )
            config_root.symlink_to(resolved_config_root, target_is_directory=True)

            with mock.patch.object(orchestration.sys, "platform", "linux"):
                paths = orchestration._codex_network_read_paths(
                    system_config_root=config_root,
                    resolver_path=config_root / "resolv.conf",
                    allowed_resolver_roots=(),
                )

        self.assertEqual((str(config_root), str(resolved_config_root.resolve())), paths)

    @unittest.skipIf(os.name == "nt", "ancestor symlink coverage requires POSIX symlinks")
    def test_codex_network_read_paths_canonicalize_an_aliased_ancestor_without_an_extra_grant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            physical_parent = temporary_root / "physical"
            config_root = physical_parent / "etc"
            config_root.mkdir(parents=True)
            resolver = config_root / "resolv.conf"
            resolver.write_text("nameserver 192.0.2.1\n", encoding="utf-8")
            aliased_parent = temporary_root / "alias"
            aliased_parent.symlink_to(physical_parent, target_is_directory=True)
            aliased_config_root = aliased_parent / "etc"

            with mock.patch.object(orchestration.sys, "platform", "linux"):
                paths = orchestration._codex_network_read_paths(
                    system_config_root=aliased_config_root,
                    resolver_path=aliased_config_root / "resolv.conf",
                    allowed_resolver_roots=(),
                )

        self.assertEqual((str(config_root.resolve()),), paths)

    def test_codex_network_read_paths_do_not_treat_a_canonical_alias_as_a_final_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            lexical_config_root = temporary_root / "lexical" / "etc"
            canonical_config_root = temporary_root / "canonical" / "etc"
            lexical_config_root.mkdir(parents=True)
            canonical_config_root.mkdir(parents=True)
            lexical_resolver = lexical_config_root / "resolv.conf"
            canonical_resolver = canonical_config_root / "resolv.conf"
            lexical_resolver.write_text("nameserver 192.0.2.1\n", encoding="utf-8")
            canonical_resolver.write_text("nameserver 192.0.2.1\n", encoding="utf-8")
            concrete_path_type = type(lexical_config_root)
            real_resolve = concrete_path_type.resolve

            def aliased_resolve(path, strict=False):
                if path == lexical_config_root:
                    return canonical_config_root
                if path == lexical_resolver:
                    return canonical_resolver
                return real_resolve(path, strict=strict)

            with mock.patch.object(orchestration.sys, "platform", "linux"), mock.patch.object(
                concrete_path_type,
                "resolve",
                autospec=True,
                side_effect=aliased_resolve,
            ):
                paths = orchestration._codex_network_read_paths(
                    system_config_root=lexical_config_root,
                    resolver_path=lexical_resolver,
                    allowed_resolver_roots=(),
                )

        self.assertEqual((str(canonical_config_root),), paths)

    @unittest.skipIf(os.name == "nt", "resolver symlink coverage requires POSIX symlinks")
    def test_codex_network_read_paths_add_only_supported_external_resolver_target(self):
        for relative_root in (Path("run/systemd/resolve"), Path("mnt/wsl")):
            with self.subTest(relative_root=relative_root), tempfile.TemporaryDirectory() as tmpdir:
                temporary_root = Path(tmpdir)
                config_root = temporary_root / "etc"
                target_root = temporary_root / relative_root
                config_root.mkdir()
                target_root.mkdir(parents=True)
                target = target_root / "resolv.conf"
                target.write_text("nameserver 192.0.2.1\n", encoding="utf-8")
                resolver = config_root / "resolv.conf"
                resolver.symlink_to(target)

                with mock.patch.object(orchestration.sys, "platform", "linux"):
                    paths = orchestration._codex_network_read_paths(
                        system_config_root=config_root,
                        resolver_path=resolver,
                        allowed_resolver_roots=(target_root,),
                    )

                self.assertEqual((str(config_root.resolve()), str(target.resolve())), paths)

    @unittest.skipIf(os.name == "nt", "resolver symlink coverage requires POSIX symlinks")
    def test_codex_network_read_paths_reject_unsupported_external_resolver_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            config_root = temporary_root / "etc"
            supported_root = temporary_root / "run" / "systemd" / "resolve"
            unsupported_root = temporary_root / "operator-data"
            config_root.mkdir()
            supported_root.mkdir(parents=True)
            unsupported_root.mkdir()
            target = unsupported_root / "resolv.conf"
            target.write_text("nameserver 192.0.2.1\n", encoding="utf-8")
            resolver = config_root / "resolv.conf"
            resolver.symlink_to(target)

            with mock.patch.object(orchestration.sys, "platform", "linux"), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "outside the supported system resolver roots",
            ):
                orchestration._codex_network_read_paths(
                    system_config_root=config_root,
                    resolver_path=resolver,
                    allowed_resolver_roots=(supported_root,),
                )

    def test_codex_network_read_paths_are_linux_specific(self):
        for platform_name in ("darwin", "win32"):
            with self.subTest(platform=platform_name), mock.patch.object(
                orchestration.sys, "platform", platform_name
            ):
                self.assertEqual(
                    (),
                    orchestration._codex_network_read_paths(
                        system_config_root=Path("/missing/system/config"),
                        resolver_path=Path("/missing/resolv.conf"),
                    ),
                )

    def test_runner_argv_is_structured_and_contains_no_bypass_flags(self):
        root = Path("/tmp/workspace with spaces")
        managed_python = orchestration._ManagedPythonRuntime(
            executable=root / ".venv" / "bin" / "python",
            read_paths=("/opt/python runtime",),
        )
        resolver_paths = ("/etc", "/run/systemd/resolve/stub-resolv.conf")
        with mock.patch.object(orchestration, "_codex_network_read_paths", return_value=resolver_paths) as network:
            codex = orchestration._codex_argv(
                "/tmp/fake tools/codex",
                root,
                Path("/tmp/schema.json"),
                Path("/tmp/result.json"),
                "gpt-test",
                allow_network=True,
                runtime_read_paths=("/opt/codex runtime",),
                managed_python=managed_python,
            )
            codex_offline = orchestration._codex_argv(
                "/tmp/fake tools/codex",
                root,
                Path("/tmp/schema.json"),
                Path("/tmp/result.json"),
                None,
                allow_network=False,
                runtime_read_paths=("/opt/codex runtime",),
                managed_python=managed_python,
            )
        claude = orchestration._claude_argv(
            "/tmp/fake tools/claude",
            root,
            "claude-test",
            allow_network=True,
        )
        claude_offline = orchestration._claude_argv(
            "/tmp/fake tools/claude",
            root,
            None,
            allow_network=False,
        )

        self.assertEqual("/tmp/fake tools/codex", codex[0])
        self.assertIn(str(root), codex)
        self.assertIn("never", codex)
        self.assertNotIn("--sandbox", codex)
        self.assertNotIn("workspace-write", codex)
        self.assertIn("--ignore-user-config", codex)
        self.assertIn("--ignore-rules", codex)
        self.assertIn("--strict-config", codex)
        self.assertIn("mcp_servers={}", codex)
        self.assertIn('web_search="disabled"', codex)
        self.assertIn('default_permissions="evidence_wiki_worker"', codex)
        self.assertIn("allow_login_shell=false", codex)
        codex_config = "\n".join(codex)
        offline_codex_config = "\n".join(codex_offline)
        self.assertIn('"/opt/codex runtime"="read"', codex_config)
        self.assertIn('"/opt/python runtime"="read"', codex_config)
        for resolver_path in resolver_paths:
            self.assertIn(f'{json.dumps(resolver_path)}="read"', codex_config)
            self.assertNotIn(f'{json.dumps(resolver_path)}="read"', offline_codex_config)
        network.assert_called_once_with()
        self.assertIn('shell_environment_policy={inherit="core"', codex_config)
        self.assertIn(
            f'{json.dumps(orchestration.MANAGED_PYTHON_ENV)}={json.dumps(str(managed_python.executable))}',
            codex_config,
        )
        self.assertIn(
            f'{json.dumps("PATH")}={json.dumps(orchestration._managed_python_search_path(managed_python))}',
            codex_config,
        )
        self.assertIn('"."="write"', codex_config)
        for protected_path in orchestration.PROTECTED_WORKSPACE_PATHS:
            self.assertIn(f'"{protected_path}"="read"', codex_config)
        self.assertIn('"runs/run-reports"="write"', codex_config)
        self.assertIn('\":tmpdir\"=\"write\"', codex_config)
        self.assertIn("enabled=true", codex_config)
        self.assertIn("enabled=false", offline_codex_config)
        self.assertIn("gpt-test", codex)
        self.assertEqual("/tmp/fake tools/claude", claude[0])
        self.assertIn("dontAsk", claude)
        self.assertIn("WebFetch,WebSearch", claude)
        self.assertIn("--strict-mcp-config", claude)
        self.assertEqual(
            {"mcpServers": {}},
            json.loads(claude[claude.index("--mcp-config") + 1]),
        )
        self.assertEqual("", claude[claude.index("--setting-sources") + 1])
        self.assertIn("claude-test", claude)
        settings = json.loads(claude[claude.index("--settings") + 1])
        self.assertTrue(settings["sandbox"]["enabled"])
        self.assertTrue(settings["sandbox"]["failIfUnavailable"])
        self.assertFalse(settings["sandbox"]["allowUnsandboxedCommands"])
        self.assertEqual([], settings["sandbox"]["excludedCommands"])
        self.assertEqual(["*"], settings["sandbox"]["network"]["allowedDomains"])
        offline_settings = json.loads(claude_offline[claude_offline.index("--settings") + 1])
        self.assertEqual([], offline_settings["sandbox"]["network"]["allowedDomains"])
        for protected_path in orchestration.PROTECTED_WORKSPACE_PATHS:
            self.assertIn(str(root / protected_path), settings["sandbox"]["filesystem"]["denyWrite"])
            for tool in ("Edit", "Write"):
                self.assertIn(f"{tool}(/{protected_path})", settings["permissions"]["deny"])
                if protected_path not in orchestration.PROTECTED_WORKSPACE_FILES:
                    self.assertIn(f"{tool}(/{protected_path}/**)", settings["permissions"]["deny"])
        self.assertIn(str(root / "docs"), settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertIn(str(root / "runs/run-reports"), settings["sandbox"]["filesystem"]["allowWrite"])
        self.assertIn("Edit(/docs)", settings["permissions"]["deny"])
        self.assertIn("WebSearch", settings["permissions"]["deny"])
        for argv in (codex, claude):
            joined = " ".join(argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", joined)
            self.assertNotIn("--dangerously-skip-permissions", joined)
            self.assertNotIn("--allow-dangerously-skip-permissions", joined)

    def test_codex_runtime_resolver_supports_nested_npm_package_with_spaces(self):
        layout = orchestration._codex_platform_layout()
        self.assertIsNotNone(layout)
        platform_package, target_triple, binary_name = layout
        with tempfile.TemporaryDirectory() as tmpdir:
            prefix = Path(tmpdir) / "npm prefix with spaces"
            package_root = prefix / "lib" / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            platform_root = package_root / "node_modules" / "@openai" / platform_package
            runtime_root = platform_root / "vendor" / target_triple
            native = runtime_root / "bin" / binary_name
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native")
            (runtime_root / "codex-resources").mkdir()
            (runtime_root / "codex-path").mkdir()
            (platform_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex"}),
                encoding="utf-8",
            )

            runtime_paths = orchestration._codex_runtime_read_paths(str(entrypoint))
            config = orchestration._codex_permission_profile_config(runtime_read_paths=runtime_paths)

        self.assertEqual((str(runtime_root.resolve()),), runtime_paths)
        self.assertIn(f'{json.dumps(runtime_paths[0])}="read"', config)
        self.assertNotIn(f'{json.dumps(str(prefix.resolve()))}="read"', config)
        self.assertNotIn(f'{json.dumps(str(package_root.resolve()))}="read"', config)

    @unittest.skipIf(os.name == "nt", "macOS/Linux virtual environments use symlinked launchers")
    def test_managed_python_resolves_framework_runtime_with_spaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace with spaces"
            interpreter = workspace / ".venv" / "bin" / "python"
            framework = temporary_root / "External Python" / "Python.framework" / "Versions" / "3.14"
            framework_interpreter = framework / "bin" / "python3.14"
            framework_interpreter.parent.mkdir(parents=True)
            framework_interpreter.write_bytes(b"python")
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(framework_interpreter)

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(workspace / ".venv")
            ), mock.patch.object(orchestration.sys, "base_prefix", str(framework)):
                runtime = orchestration._managed_python_runtime(workspace)

        self.assertEqual(interpreter.parent.resolve() / interpreter.name, runtime.executable)
        self.assertEqual((str(framework.resolve()),), runtime.read_paths)

    @unittest.skipIf(os.name == "nt", "POSIX-style runtime layout")
    def test_managed_python_resolves_linux_style_runtime_without_broad_prefix_grant(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace with spaces"
            interpreter = workspace / ".venv" / "bin" / "python"
            external_prefix = temporary_root / "External Python"
            external_interpreter = external_prefix / "bin" / "python3.14"
            stdlib = external_prefix / "lib" / "python3.14"
            shared_library = external_prefix / "lib" / "libpython3.14.so"
            site_packages = stdlib / "site-packages"
            external_interpreter.parent.mkdir(parents=True)
            external_interpreter.write_bytes(b"python")
            site_packages.mkdir(parents=True)
            shared_library.write_bytes(b"python library")
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(external_interpreter)
            configured_paths = {
                "stdlib": str(stdlib),
                "platstdlib": str(workspace / ".venv" / "lib" / "python3.14"),
                "purelib": str(site_packages),
                "platlib": str(site_packages),
            }

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(workspace / ".venv")
            ), mock.patch.object(orchestration.sys, "base_prefix", str(external_prefix)), mock.patch.object(
                orchestration.sysconfig, "get_path", side_effect=configured_paths.get
            ), mock.patch.object(
                orchestration.sysconfig,
                "get_config_var",
                side_effect=lambda name: {
                    "LIBDIR": str(shared_library.parent),
                    "LDLIBRARY": shared_library.name,
                }.get(name),
            ):
                runtime = orchestration._managed_python_runtime(workspace)

        self.assertEqual(interpreter.parent.resolve() / interpreter.name, runtime.executable)
        self.assertEqual(
            {
                str(external_interpreter.resolve()),
                str(stdlib.resolve()),
                str(shared_library.resolve()),
            },
            set(runtime.read_paths),
        )
        self.assertNotIn(str(external_prefix.resolve()), runtime.read_paths)

    @unittest.skipUnless(os.name == "nt", "native Windows virtual-environment layout")
    def test_managed_python_resolves_windows_base_runtime(self):  # pragma: no cover - Windows CI
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace with spaces"
            interpreter = workspace / ".venv" / "Scripts" / "python.exe"
            base_prefix = temporary_root / "External Python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            base_prefix.mkdir()

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(workspace / ".venv")
            ), mock.patch.object(orchestration.sys, "base_prefix", str(base_prefix)):
                runtime = orchestration._managed_python_runtime(workspace)

        self.assertEqual(interpreter.parent.resolve() / interpreter.name, runtime.executable)
        self.assertEqual((str(base_prefix.resolve()),), runtime.read_paths)

    def test_managed_python_rejects_unprotected_workspace_interpreter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            interpreter = workspace / "tools" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*outside the protected",
            ):
                orchestration._managed_python_runtime(workspace)

    def test_managed_python_rejects_workspace_runtime_files_outside_protected_venv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace"
            interpreter = workspace / ".venv" / "bin" / "python"
            unprotected_stdlib = workspace / "Lib"
            external_base = temporary_root / "External Python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            unprotected_stdlib.mkdir()
            external_base.mkdir()
            configured_paths = {
                "stdlib": str(unprotected_stdlib),
                "platstdlib": str(workspace / ".venv" / "lib"),
                "purelib": str(workspace / ".venv" / "site-packages"),
                "platlib": str(workspace / ".venv" / "site-packages"),
            }

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(workspace / ".venv")
            ), mock.patch.object(orchestration.sys, "base_prefix", str(external_base)), mock.patch.object(
                orchestration.sysconfig, "get_path", side_effect=configured_paths.get
            ), mock.patch.object(
                orchestration.sysconfig, "get_config_var", return_value=None
            ), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*runtime files outside the protected",
            ):
                orchestration._managed_python_runtime(workspace)

    def test_managed_python_validates_windows_sysconfig_paths_against_workspace(self):
        """Windows grants base_prefix, but must still reject a workspace Lib."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace"
            interpreter = workspace / ".venv" / "Scripts" / "python.exe"
            unprotected_stdlib = workspace / "Lib"
            external_base = temporary_root / "External Python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")
            unprotected_stdlib.mkdir()
            external_base.mkdir()
            configured_paths = {
                "stdlib": str(unprotected_stdlib),
                "platstdlib": str(workspace / ".venv" / "Lib"),
                "purelib": str(workspace / ".venv" / "Lib" / "site-packages"),
                "platlib": str(workspace / ".venv" / "Lib" / "site-packages"),
            }

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(workspace / ".venv")
            ), mock.patch.object(orchestration.sys, "base_prefix", str(external_base)), mock.patch.object(
                orchestration.sysconfig, "get_path", side_effect=configured_paths.get
            ), mock.patch.object(
                orchestration.sysconfig, "get_config_var", return_value=None
            ), mock.patch.object(
                orchestration, "_is_native_windows", return_value=True
            ), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*runtime files outside the protected",
            ):
                orchestration._managed_python_runtime(workspace)

    @unittest.skipIf(os.name == "nt", "Windows workspace profile matching is case-insensitive")
    def test_managed_python_rejects_case_variant_venv_on_posix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            interpreter = workspace / ".VENV" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*outside the protected",
            ):
                orchestration._managed_python_runtime(workspace)

    def test_managed_python_rejects_runtime_path_containing_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temporary_root = Path(tmpdir)
            workspace = temporary_root / "workspace"
            workspace.mkdir()
            home_parent = temporary_root / "Users"
            fake_home = home_parent / "operator"
            fake_home.mkdir(parents=True)
            framework = temporary_root / "External" / "Python.framework" / "Versions" / "3.14"
            interpreter = framework / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), mock.patch.object(
                orchestration.sys, "prefix", str(home_parent)
            ), mock.patch.object(orchestration.sys, "base_prefix", str(framework)), mock.patch.object(
                orchestration.Path, "home", return_value=fake_home
            ), mock.patch.dict(os.environ, {"CODEX_HOME": ""}, clear=False), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*over-broad Python runtime read path",
            ):
                orchestration._managed_python_runtime(workspace)

    @unittest.skipIf(os.name == "nt", "POSIX PATH separator behavior")
    def test_managed_python_rejects_path_separator_in_interpreter_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace:unsafe"
            interpreter = workspace / ".venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b"python")

            with mock.patch.object(orchestration.sys, "executable", str(interpreter)), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*represented safely on PATH",
            ):
                orchestration._managed_python_runtime(workspace)

    def test_runner_prompt_uses_native_windows_managed_python_syntax(self):
        with mock.patch.object(orchestration, "_is_native_windows", return_value=True):
            prompt = orchestration._runner_prompt(work_order())

        self.assertIn(f'& "$env:{orchestration.MANAGED_PYTHON_ENV}" -B scripts/...', prompt)
        self.assertIn(f'"%{orchestration.MANAGED_PYTHON_ENV}%" -B scripts/...', prompt)
        self.assertIn("replace only that executable", prompt)

    def test_runner_executable_canonicalizes_a_relative_path_before_workspace_cwd_changes(self):
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as tmpdir:
            executable = Path(tmpdir) / "codex"
            executable.write_bytes(b"native")
            executable.chmod(0o755)
            relative = os.path.relpath(executable, Path.cwd())

            with mock.patch.object(orchestration.shutil, "which", return_value=relative):
                selected = orchestration._runner_executable("codex")

        self.assertEqual(str(executable.resolve()), selected)

    def test_codex_runtime_resolver_uses_the_installed_platform_package_not_python_architecture(self):
        layouts = orchestration._codex_host_platform_layouts()
        self.assertGreaterEqual(len(layouts), 2)
        platform_package, target_triple, binary_name = layouts[-1]
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            platform_root = package_root / "node_modules" / "@openai" / platform_package
            runtime_root = platform_root / "vendor" / target_triple
            native = runtime_root / "bin" / binary_name
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native")
            (platform_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex"}),
                encoding="utf-8",
            )

            with mock.patch.object(orchestration.platform, "machine", return_value="x86_64"):
                runtime_paths = orchestration._codex_runtime_read_paths(str(entrypoint))

        self.assertEqual((str(runtime_root.resolve()),), runtime_paths)

    def test_codex_runtime_resolver_fails_closed_for_ambiguous_installed_architectures(self):
        layouts = orchestration._codex_host_platform_layouts()
        self.assertGreaterEqual(len(layouts), 2)
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            for platform_package, target_triple, binary_name in layouts[:2]:
                platform_root = package_root / "node_modules" / "@openai" / platform_package
                native = platform_root / "vendor" / target_triple / "bin" / binary_name
                native.parent.mkdir(parents=True)
                native.write_bytes(b"native")
                (platform_root / "package.json").write_text(
                    json.dumps({"name": "@openai/codex"}),
                    encoding="utf-8",
                )

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*ambiguous installed platform runtimes",
            ):
                orchestration._codex_runtime_read_paths(str(entrypoint))

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_codex_runtime_resolver_rejects_a_native_binary_symlink_escape(self):
        platform_package, target_triple, binary_name = orchestration._codex_host_platform_layouts()[0]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package_root = root / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            platform_root = package_root / "node_modules" / "@openai" / platform_package
            native = platform_root / "vendor" / target_triple / "bin" / binary_name
            native.parent.mkdir(parents=True)
            escaped_native = root / "outside" / binary_name
            escaped_native.parent.mkdir()
            escaped_native.write_bytes(b"native")
            native.symlink_to(escaped_native)
            (platform_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*escapes its package root",
            ):
                orchestration._codex_runtime_read_paths(str(entrypoint))

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_codex_runtime_resolver_rejects_a_symlinked_package_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package_root = root / "node_modules" / "@openai" / "codex"
            package_root.mkdir(parents=True)
            external_manifest = root / "package.json"
            external_manifest.write_text(json.dumps({"name": "@openai/codex"}), encoding="utf-8")
            (package_root / "package.json").symlink_to(external_manifest)

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*unsafe or unbounded.*manifest",
            ):
                orchestration._read_codex_package_manifest(package_root, required=True)

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliable on Windows CI")
    def test_codex_runtime_resolver_follows_pnpm_platform_package_symlink(self):
        layout = orchestration._codex_platform_layout()
        self.assertIsNotNone(layout)
        platform_package, target_triple, binary_name = layout
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            package_root = root / "pnpm" / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            store_package = root / "pnpm store" / platform_package
            runtime_root = store_package / "vendor" / target_triple
            native = runtime_root / "bin" / binary_name
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native")
            (runtime_root / "codex-resources").mkdir()
            (store_package / "package.json").write_text(
                json.dumps({"name": "@openai/codex"}),
                encoding="utf-8",
            )
            dependency_parent = package_root / "node_modules" / "@openai"
            dependency_parent.mkdir(parents=True)
            (dependency_parent / platform_package).symlink_to(store_package, target_is_directory=True)

            runtime_paths = orchestration._codex_runtime_read_paths(str(entrypoint))

        self.assertEqual((str(runtime_root.resolve()),), runtime_paths)

    def test_codex_runtime_resolver_supports_windows_npm_cmd_shim(self):
        platform_package, target_triple, binary_name = orchestration.CODEX_PLATFORM_LAYOUTS[("win32", "x64")]
        with tempfile.TemporaryDirectory() as tmpdir:
            shim_root = Path(tmpdir) / "npm bin"
            shim_root.mkdir()
            shim = shim_root / "codex.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            package_root = shim_root / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("// codex\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )
            platform_root = package_root / "node_modules" / "@openai" / platform_package
            runtime_root = platform_root / "vendor" / target_triple
            native = runtime_root / "bin" / binary_name
            native.parent.mkdir(parents=True)
            native.write_bytes(b"native")
            (runtime_root / "codex-resources").mkdir()
            (platform_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex"}),
                encoding="utf-8",
            )

            with mock.patch.object(orchestration.sys, "platform", "win32"), mock.patch.object(
                orchestration.platform, "machine", return_value="AMD64"
            ):
                runtime_paths = orchestration._codex_runtime_read_paths(str(shim))
                with mock.patch.object(orchestration.shutil, "which", return_value=str(shim)), mock.patch.object(
                    orchestration, "_is_native_windows", return_value=True
                ):
                    selected_executable = orchestration._runner_executable("codex")

        self.assertEqual((str(runtime_root.resolve()),), runtime_paths)
        self.assertEqual(str(native.resolve()), selected_executable)
        self.assertFalse(selected_executable.casefold().endswith((".bat", ".cmd", ".ps1")))

    def test_codex_runtime_resolver_supports_direct_ide_tree_and_quotes_toml_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_root = Path(tmpdir) / "Codex preview runtime"
            runtime_root.mkdir()
            (runtime_root / "codex-resources").mkdir()
            (runtime_root / "codex-path").mkdir()
            native = runtime_root / ("codex.exe" if os.name == "nt" else "codex")
            native.write_bytes(b"native")

            runtime_paths = orchestration._codex_runtime_read_paths(str(native))
            config = orchestration._codex_permission_profile_config(runtime_read_paths=runtime_paths)

        self.assertEqual((str(runtime_root.resolve()),), runtime_paths)
        self.assertIn(f'{json.dumps(runtime_paths[0])}="read"', config)
        windows_path = 'C:\\Users\\Mike Doe\\Codex "preview"\\runtime'
        windows_config = orchestration._codex_permission_profile_config(runtime_read_paths=(windows_path,))
        self.assertIn(f'{json.dumps(windows_path)}="read"', windows_config)

    def test_bounded_codex_manifest_read_accepts_descriptor_timestamp_representation_differences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "package.json"
            expected = b'{"name":"@openai/codex"}'
            manifest.write_bytes(expected)
            real_fstat = os.fstat

            def shifted_descriptor_stat(descriptor):
                metadata = real_fstat(descriptor)
                return mock.Mock(
                    st_mode=metadata.st_mode,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_size=metadata.st_size,
                    st_mtime_ns=metadata.st_mtime_ns + 1,
                    st_ctime_ns=metadata.st_ctime_ns + 1,
                )

            with mock.patch.object(orchestration.os, "fstat", side_effect=shifted_descriptor_stat):
                observed = orchestration._bounded_regular_file_bytes(
                    manifest,
                    max_bytes=orchestration.MAX_CODEX_PACKAGE_JSON_BYTES,
                    label="test package manifest",
                )

        self.assertEqual(expected, observed)

    def test_bounded_codex_manifest_read_rejects_same_descriptor_metadata_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = Path(tmpdir) / "package.json"
            manifest.write_bytes(b'{"name":"@openai/codex"}')
            real_fstat = os.fstat
            calls = 0

            def changing_descriptor_stat(descriptor):
                nonlocal calls
                metadata = real_fstat(descriptor)
                calls += 1
                return mock.Mock(
                    st_mode=metadata.st_mode,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                    st_size=metadata.st_size,
                    st_mtime_ns=metadata.st_mtime_ns + (1 if calls > 1 else 0),
                    st_ctime_ns=metadata.st_ctime_ns,
                )

            with mock.patch.object(
                orchestration.os,
                "fstat",
                side_effect=changing_descriptor_stat,
            ), self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*changed while it was read",
            ):
                orchestration._bounded_regular_file_bytes(
                    manifest,
                    max_bytes=orchestration.MAX_CODEX_PACKAGE_JSON_BYTES,
                    label="test package manifest",
                )

    def test_codex_runtime_resolver_grants_only_an_unknown_direct_executable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executable = Path(tmpdir) / "standalone-codex"
            executable.write_bytes(b"native")

            runtime_paths = orchestration._codex_runtime_read_paths(str(executable))

        self.assertEqual((str(executable.resolve()),), runtime_paths)

    def test_codex_runtime_resolution_fails_closed_for_missing_platform_package(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text(
                json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*platform runtime.*reinstall",
            ):
                orchestration._codex_runtime_read_paths(str(entrypoint))

    def test_codex_runtime_resolution_fails_closed_for_malformed_official_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            package_root = Path(tmpdir) / "node_modules" / "@openai" / "codex"
            entrypoint = package_root / "bin" / "codex.js"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (package_root / "package.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*manifest.*reinstall",
            ):
                orchestration._codex_runtime_read_paths(str(entrypoint))

    def test_codex_runtime_may_not_overlap_the_writable_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            runtime = workspace / "runner"
            runtime.mkdir(parents=True)

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*overlaps",
            ):
                orchestration._validate_codex_runtime_workspace_boundary((str(runtime),), workspace)

    def test_codex_launcher_and_package_may_not_overlap_the_writable_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            package_root = workspace / "node_modules" / "@openai" / "codex"
            launcher = package_root / "bin" / "codex.js"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            external_runtime = Path(tmpdir) / "external-runtime"
            external_runtime.mkdir()

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "RUNNER_ISOLATION_UNAVAILABLE.*launcher overlaps",
            ):
                orchestration._validate_codex_runtime_workspace_boundary(
                    (str(external_runtime),),
                    workspace,
                    launcher_path=launcher,
                    package_root=package_root,
                )

    def test_codex_capability_probe_requires_supported_version_and_enforces_profile(self):
        observed = []
        managed_python = orchestration._ManagedPythonRuntime(
            executable=Path("/opt/python runtime/bin/python"),
            read_paths=("/opt/python runtime",),
        )

        def capability(argv, *, cwd):
            observed.append((argv, cwd))
            if argv[1:] == ["--version"]:
                return orchestration.ProcessResult(0, "codex-cli 0.144.6\n", "")
            self.assertEqual("sandbox", argv[1])
            if argv[-5:] == [
                str(managed_python.executable),
                "-I",
                "-B",
                "-c",
                orchestration.MANAGED_PYTHON_PROBE,
            ]:
                self.assertEqual(REPO_ROOT, cwd)
                return orchestration.ProcessResult(0, "", "")
            self.assertEqual("trusted\n", (cwd / "protected" / "sentinel.txt").read_text(encoding="utf-8"))
            if os.name != "nt" and not (hasattr(os, "geteuid") and os.geteuid() == 0):
                protected = cwd / "protected"
                sentinel = protected / "sentinel.txt"
                protected.chmod(0o555)
                sentinel.chmod(0o444)
                try:
                    probe = subprocess.run(  # noqa: S603 - fixed host-generated probe command.
                        argv[-3:],
                        cwd=cwd,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                finally:
                    protected.chmod(0o755)
                    sentinel.chmod(0o644)
                self.assertEqual(0, probe.returncode, probe.stderr)
            else:
                (cwd / "allowed.txt").write_text("allowed", encoding="utf-8")
            return orchestration.ProcessResult(0, "", "")

        runtime_resolution = orchestration._CodexRuntimeResolution(
            launcher=Path("/tmp/fake codex"),
            package_root=None,
            native_binary=Path("/opt/codex-runtime/bin/codex"),
            runtime_root=Path("/opt/codex-runtime"),
        )
        with mock.patch.object(
            orchestration, "_codex_runtime_resolution", return_value=runtime_resolution
        ), mock.patch.object(
            orchestration, "_managed_python_runtime", return_value=managed_python
        ), mock.patch.object(
            orchestration, "_validate_codex_runtime_workspace_boundary"
        ), mock.patch.object(orchestration, "_run_runner_capability_command", side_effect=capability):
            orchestration._validate_runner_capability("codex", "/tmp/fake codex", REPO_ROOT)

        self.assertEqual(3, len(observed))
        probe_argv = observed[1][0]
        self.assertIn("--permission-profile", probe_argv)
        self.assertEqual(
            orchestration.CODEX_PERMISSION_PROFILE_NAME,
            probe_argv[probe_argv.index("--permission-profile") + 1],
        )
        self.assertNotIn("--sandbox", probe_argv)
        self.assertIn(
            f'{json.dumps(str(runtime_resolution.runtime_root))}="read"',
            "\n".join(probe_argv),
        )
        self.assertIn(
            f'{json.dumps(managed_python.read_paths[0])}="read"',
            "\n".join(probe_argv),
        )
        if os.name != "nt":
            self.assertEqual("/bin/sh", probe_argv[-3])
            self.assertNotIn(sys.executable, probe_argv)
        python_probe_argv = observed[2][0]
        self.assertIn("allow_login_shell=false", python_probe_argv)
        self.assertIn("shell_environment_policy={inherit=\"core\"", "\n".join(python_probe_argv))
        self.assertEqual(
            [
                str(managed_python.executable),
                "-I",
                "-B",
                "-c",
                orchestration.MANAGED_PYTHON_PROBE,
            ],
            python_probe_argv[-5:],
        )
        self.assertIn('"."="read"', "\n".join(python_probe_argv))
        self.assertNotIn('"."="write"', "\n".join(python_probe_argv))
        self.assertIn("pypdf", python_probe_argv[-1])
        self.assertIn("ssl.create_default_context()", python_probe_argv[-1])
        self.assertIn(orchestration.MANAGED_PYTHON_ENV, python_probe_argv[-1])

        with mock.patch.object(
            orchestration, "_codex_runtime_resolution", return_value=runtime_resolution
        ), mock.patch.object(
            orchestration, "_managed_python_runtime", return_value=managed_python
        ), mock.patch.object(
            orchestration, "_validate_codex_runtime_workspace_boundary"
        ), mock.patch.object(
            orchestration,
            "_run_runner_capability_command",
            return_value=orchestration.ProcessResult(0, "codex-cli 0.137.9\n", ""),
        ), self.assertRaisesRegex(orchestration.OrchestrationHostError, "RUNNER_ISOLATION_UNAVAILABLE.*0.138.0"):
            orchestration._validate_runner_capability("codex", "/tmp/fake codex", REPO_ROOT)

    def test_codex_capability_probe_fails_before_launch_when_managed_python_is_unavailable(self):
        managed_python = orchestration._ManagedPythonRuntime(
            executable=Path("/opt/python runtime/bin/python"),
            read_paths=("/opt/python runtime",),
        )
        failed = orchestration.ProcessResult(1, "", "dyld: library not loaded")

        with mock.patch.object(
            orchestration,
            "_run_runner_capability_command",
            return_value=failed,
        ) as capability, self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "RUNNER_ISOLATION_UNAVAILABLE.*selected Python interpreter.*PyYAML.*pypdf.*TLS.*recreate",
        ) as caught:
            orchestration._probe_codex_managed_python(
                "/tmp/fake codex",
                REPO_ROOT,
                ("/opt/codex runtime", *managed_python.read_paths),
                managed_python,
            )

        argv = capability.call_args.args[0]
        self.assertEqual(
            [
                str(managed_python.executable),
                "-I",
                "-B",
                "-c",
                orchestration.MANAGED_PYTHON_PROBE,
            ],
            argv[-5:],
        )
        self.assertIn("dyld: library not loaded", str(caught.exception))

    def test_codex_capability_probe_fails_closed_when_profile_is_not_enforced(self):
        def capability(argv, *, cwd):
            if argv[1:] == ["--version"]:
                return orchestration.ProcessResult(0, "codex-cli 0.144.6\n", "")
            (cwd / "allowed.txt").write_text("allowed", encoding="utf-8")
            (cwd / "protected" / "sentinel.txt").write_text("tampered", encoding="utf-8")
            return orchestration.ProcessResult(0, "", "")

        runtime_resolution = orchestration._CodexRuntimeResolution(
            launcher=Path("/tmp/fake codex"),
            package_root=None,
            native_binary=Path("/opt/codex-runtime/bin/codex"),
            runtime_root=Path("/opt/codex-runtime"),
        )
        with mock.patch.object(
            orchestration, "_codex_runtime_resolution", return_value=runtime_resolution
        ), mock.patch.object(
            orchestration, "_validate_codex_runtime_workspace_boundary"
        ), mock.patch.object(
            orchestration,
            "_run_runner_capability_command",
            side_effect=capability,
        ), self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "RUNNER_ISOLATION_UNAVAILABLE.*permission-profile sandbox",
        ):
            orchestration._validate_runner_capability("codex", "/tmp/fake codex", REPO_ROOT)

    def test_claude_capability_probe_checks_linux_primitives_and_cli_flags(self):
        observed = []

        def which(name):
            return {"bwrap": "/usr/bin/bwrap", "socat": "/usr/bin/socat"}.get(name)

        def capability(argv, *, cwd):
            observed.append(argv)
            if argv[0] == "/usr/bin/bwrap":
                (cwd / "allowed" / "sandbox-ok").write_text("ok", encoding="utf-8")
                return orchestration.ProcessResult(0, "", "")
            return orchestration.ProcessResult(
                0,
                "--json-schema --settings --setting-sources --strict-mcp-config",
                "",
            )

        with mock.patch.object(orchestration, "_is_native_windows", return_value=False), mock.patch.object(
            orchestration.sys, "platform", "linux"
        ), mock.patch.object(
            orchestration.shutil, "which", side_effect=which
        ), mock.patch.object(orchestration, "_run_runner_capability_command", side_effect=capability):
            orchestration._validate_runner_capability("claude", "/tmp/fake claude", REPO_ROOT)

        self.assertEqual("/usr/bin/bwrap", observed[0][0])
        self.assertEqual(2, observed[0].count("--ro-bind"))
        self.assertEqual(["/tmp/fake claude", "--help"], observed[1])

    def test_claude_capability_probe_rejects_a_non_enforcing_primitive(self):
        def which(name):
            return {"bwrap": "/usr/bin/bwrap", "socat": "/usr/bin/socat"}.get(name)

        def capability(_argv, *, cwd):
            (cwd / "allowed" / "sandbox-ok").write_text("ok", encoding="utf-8")
            (cwd / "protected" / "sentinel.txt").write_text("tampered", encoding="utf-8")
            return orchestration.ProcessResult(0, "", "")

        with mock.patch.object(orchestration, "_is_native_windows", return_value=False), mock.patch.object(
            orchestration.sys, "platform", "linux"
        ), mock.patch.object(
            orchestration.shutil, "which", side_effect=which
        ), mock.patch.object(
            orchestration, "_run_runner_capability_command", side_effect=capability
        ), self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "RUNNER_ISOLATION_UNAVAILABLE.*sandbox primitives failed",
        ):
            orchestration._validate_runner_capability("claude", "/tmp/fake claude", REPO_ROOT)

    def test_claude_capability_fails_closed_on_native_windows_or_missing_primitives(self):
        with mock.patch.object(orchestration, "_is_native_windows", return_value=True), mock.patch.object(
            orchestration, "_run_runner_capability_command"
        ) as capability, self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "RUNNER_ISOLATION_UNAVAILABLE.*native Windows.*WSL2",
        ):
            orchestration._validate_runner_capability("claude", "C:/fake/claude.exe", REPO_ROOT)
        capability.assert_not_called()

        with mock.patch.object(orchestration, "_is_native_windows", return_value=False), mock.patch.object(
            orchestration.sys, "platform", "linux"
        ), mock.patch.object(
            orchestration.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/bwrap" if name == "bwrap" else None,
        ), self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "RUNNER_ISOLATION_UNAVAILABLE.*socat",
        ):
            orchestration._validate_runner_capability("claude", "/tmp/fake claude", REPO_ROOT)

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

    def test_action_timeout_is_capped_by_absolute_lease_expiry(self):
        order = work_order()
        order["lease"]["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIn(orchestration._action_timeout(order, 60), range(1, 6))

        order["lease"]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "ORCHESTRATION_LEASE_EXPIRED",
        ):
            orchestration._action_timeout(order, 60)

    def test_running_attempt_blocks_worker_replay_until_lease_is_renewed(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            orchestration._start_attempt(root, order, "codex")

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return order
                raise AssertionError(command)

            with mock.patch.object(
                orchestration,
                "_controller_json",
                side_effect=controller,
            ), mock.patch.object(
                orchestration,
                "_runner_executable",
            ) as runner, mock.patch.object(
                orchestration,
                "execute_work_order",
            ) as execute, self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "ORCHESTRATION_LEASE_ACTIVE",
            ):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            runner.assert_not_called()
            execute.assert_not_called()
            renewed = json.loads(json.dumps(order))
            renewed["lease"]["attempt"] = 2
            orchestration._refuse_overlapping_running_attempt(root, renewed)

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

    def test_darwin_process_group_quiescence_requires_valid_process_table_without_live_members(self):
        cases = (
            ("4321 S\n1234 Z\n1234 Z+\n", True),
            ("4321 S\n1234 S\n", False),
            ("malformed\n", False),
            ("", False),
        )
        for output, expected in cases:
            with self.subTest(output=output), mock.patch.object(
                orchestration.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
            ):
                self.assertEqual(expected, orchestration._darwin_process_group_is_quiescent(1234))

    def test_darwin_process_group_quiescence_fails_closed_when_process_table_is_unavailable(self):
        with mock.patch.object(
            orchestration.subprocess,
            "run",
            side_effect=PermissionError("denied"),
        ):
            self.assertFalse(orchestration._darwin_process_group_is_quiescent(1234))

    @unittest.skipUnless(os.name == "posix", "process-group signaling is POSIX-specific")
    def test_process_group_cleanup_ignores_permission_error_after_runner_exit(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = -1

        with mock.patch.object(orchestration.sys, "platform", "darwin"), mock.patch.object(
            orchestration.os,
            "killpg",
            side_effect=PermissionError("denied"),
        ), mock.patch.object(
            orchestration,
            "_darwin_process_group_is_quiescent",
            return_value=True,
        ) as quiescent:
            orchestration._terminate_process_group(process, force=True)

        process.poll.assert_called_once_with()
        quiescent.assert_called_once_with(1234)

    @unittest.skipUnless(os.name == "posix", "process-group signaling is POSIX-specific")
    def test_process_group_cleanup_propagates_permission_error_for_live_runner(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = None

        with mock.patch.object(orchestration.sys, "platform", "darwin"), mock.patch.object(
            orchestration.os,
            "killpg",
            side_effect=PermissionError("denied"),
        ), mock.patch.object(
            orchestration,
            "_darwin_process_group_is_quiescent",
        ) as quiescent, self.assertRaises(PermissionError):
            orchestration._terminate_process_group(process, force=True)

        quiescent.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process-group signaling is POSIX-specific")
    def test_process_group_cleanup_propagates_permission_error_with_live_darwin_descendants(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = -1

        with mock.patch.object(orchestration.sys, "platform", "darwin"), mock.patch.object(
            orchestration.os,
            "killpg",
            side_effect=PermissionError("denied"),
        ), mock.patch.object(
            orchestration,
            "_darwin_process_group_is_quiescent",
            return_value=False,
        ), self.assertRaises(PermissionError):
            orchestration._terminate_process_group(process, force=True)

    @unittest.skipUnless(os.name == "posix", "process-group signaling is POSIX-specific")
    def test_process_group_cleanup_propagates_post_exit_permission_error_off_macos(self):
        process = mock.Mock(pid=1234)
        process.poll.return_value = -1

        with mock.patch.object(orchestration.sys, "platform", "linux"), mock.patch.object(
            orchestration.os,
            "killpg",
            side_effect=PermissionError("denied"),
        ), self.assertRaises(PermissionError):
            orchestration._terminate_process_group(process, force=True)

        process.poll.assert_not_called()

    def test_bounded_execution_preserves_primary_process_group_cleanup_error(self):
        primary = PermissionError("primary cleanup failure")
        retry = PermissionError("retry cleanup failure")

        with mock.patch.object(
            orchestration,
            "_terminate_process_group",
            side_effect=(primary, retry),
        ) as terminate, self.assertRaisesRegex(PermissionError, "primary cleanup failure"):
            orchestration._execute_bounded(
                [sys.executable, "-c", "pass"],
                cwd=REPO_ROOT,
                stdin_text="",
                timeout_seconds=10,
            )

        self.assertEqual(2, terminate.call_count)

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
        self.assertEqual("-B", argv[1])
        self.assertEqual(str(root / "scripts" / "orchestration_controller.py"), argv[2])
        self.assertEqual(str(root), run.call_args.kwargs["cwd"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual("1", run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"])

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

    def test_managed_result_canonicalization_drops_only_host_owned_artifacts(self):
        document = result()
        document["artifacts"] = [
            "wiki/questions/question-1.md",
            "runs/orchestrations/orch-1/session.json",
            "./RUNS/ORCHESTRATIONS/orch-1/work-results/action-0001.json",
        ]

        canonical = orchestration._canonicalize_managed_result(document)

        self.assertEqual(["wiki/questions/question-1.md"], canonical["artifacts"])
        self.assertEqual(3, len(document["artifacts"]))
        self.assertEqual(canonical, orchestration._validate_result(canonical, "action-0001"))

    def test_managed_result_canonicalization_does_not_hide_other_invalid_artifacts(self):
        invalid_documents = []

        unsafe = result()
        unsafe["artifacts"] = ["runs/orchestrations/orch-1/session.json", "../outside.md"]
        invalid_documents.append(unsafe)

        duplicate = result()
        duplicate["artifacts"] = [
            "runs/orchestrations/orch-1/session.json",
            "runs/orchestrations/orch-1/session.json",
        ]
        invalid_documents.append(duplicate)

        over_limit = result()
        over_limit["artifacts"] = [
            *[f"wiki/results/{index}.md" for index in range(256)],
            "runs/orchestrations/orch-1/session.json",
        ]
        invalid_documents.append(over_limit)

        for document in invalid_documents:
            with self.subTest(artifacts=document["artifacts"][-2:]), self.assertRaises(
                orchestration.OrchestrationHostError
            ):
                orchestration._validate_result(
                    orchestration._canonicalize_managed_result(document),
                    "action-0001",
                )

    def test_result_validation_rejects_environment_credentials(self):
        document = result()
        document["summary"] = "accidentally copied secret-value-123"
        with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret-value-123"}, clear=False):
            with self.assertRaises(orchestration.OrchestrationHostError):
                orchestration._validate_result(document, "action-0001")

    def test_result_validation_enforces_constraints_omitted_from_runner_schema(self):
        invalid_documents = []

        blank_summary = result()
        blank_summary["summary"] = ""
        invalid_documents.append(("blank summary", blank_summary))

        overlong_summary = result()
        overlong_summary["summary"] = "x" * 4001
        invalid_documents.append(("overlong summary", overlong_summary))

        too_many_artifacts = result()
        too_many_artifacts["artifacts"] = [f"wiki/results/{index}.md" for index in range(257)]
        invalid_documents.append(("too many artifacts", too_many_artifacts))

        overlong_path = result()
        overlong_path["artifacts"] = ["wiki/results/" + ("x" * 500)]
        invalid_documents.append(("overlong path", overlong_path))

        duplicate_paths = result()
        duplicate_paths["artifacts"] = ["wiki/results/result.md", "wiki/results/result.md"]
        invalid_documents.append(("duplicate paths", duplicate_paths))

        for label, document in invalid_documents:
            with self.subTest(case=label), self.assertRaises(orchestration.OrchestrationHostError):
                orchestration._validate_result(document, "action-0001")

    def test_runner_diagnostics_redact_url_encoded_environment_credentials(self):
        with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret/value+123"}, clear=False):
            diagnostic = "https://api.openalex.org/works?api_key=secret%2Fvalue%2B123"
            redacted = orchestration._redact(diagnostic)
        self.assertNotIn("secret/value+123", redacted)
        self.assertNotIn("secret%2Fvalue%2B123", redacted)
        self.assertIn("<redacted>", redacted)

    def test_runner_policy_argv_never_serializes_environment_credentials(self):
        root = Path("/tmp/workspace")
        managed_python = orchestration._ManagedPythonRuntime(
            executable=root / ".venv" / "bin" / "python",
            read_paths=("/opt/python-runtime",),
        )
        with mock.patch.dict(
            os.environ,
            {"OPENALEX_API_KEY": "openalex-secret-value", "GITHUB_TOKEN": "github-secret-value"},
            clear=False,
        ):
            codex = orchestration._codex_argv(
                "/tmp/codex",
                root,
                Path("/tmp/schema.json"),
                Path("/tmp/result.json"),
                None,
                allow_network=True,
                runtime_read_paths=("/opt/codex-runtime",),
                managed_python=managed_python,
            )
            claude = orchestration._claude_argv("/tmp/claude", root, None, allow_network=True)

        serialized = json.dumps({"codex": codex, "claude": claude})
        self.assertNotIn("openalex-secret-value", serialized)
        self.assertNotIn("github-secret-value", serialized)

    def test_codex_result_is_read_from_schema_constrained_output_file(self):
        observed = {}

        def fake_execute(argv, **kwargs):
            observed["argv"] = argv
            observed["prompt"] = kwargs["stdin_text"]
            observed["environment"] = kwargs["environment"]
            schema_path = Path(argv[argv.index("--output-schema") + 1])
            observed["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
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
                runtime_read_paths=("/opt/codex-runtime",),
            )

        self.assertEqual(result(), document)
        self.assertEqual("/tmp/fake codex", observed["argv"][0])
        self.assertIn("--output-schema", observed["argv"])
        self.assertIn('"action_id": "action-0001"', observed["prompt"])
        self.assertIn("blocked_on_sources after creating structured source requests is completed", observed["prompt"])
        self.assertIn("blocked: the bounded action cannot currently complete", observed["prompt"])
        self.assertIn("leave a scoped acquisition candidate selected", observed["prompt"])
        self.assertIn("selected to failed", observed["prompt"])
        self.assertIn("This action may be a replay after interruption", observed["prompt"])
        self.assertIn("hard authorization and boundedness limits", observed["prompt"])
        self.assertIn("never duplicate downloads", observed["prompt"])
        self.assertIn("entire runs/orchestrations/ tree", observed["prompt"])
        self.assertIn("Never include runs/orchestrations or any descendant in artifacts", observed["prompt"])
        self.assertIn("use an empty artifacts list", observed["prompt"])
        self.assertIn("Generated run reports belong under runs/run-reports/", observed["prompt"])
        self.assertIn("Do not start background processes", observed["prompt"])
        self.assertIn(orchestration.MANAGED_PYTHON_ENV, observed["prompt"])
        self.assertIn("never use bare python, python3, or py", observed["prompt"])
        self.assertNotIn(str(REPO_ROOT), observed["prompt"])
        self.assertEqual(
            sys.executable,
            observed["environment"][orchestration.MANAGED_PYTHON_ENV],
        )

        schema = observed["schema"]
        self.assertEqual(orchestration.ORCHESTRATION_RESULT_SCHEMA, schema)
        properties = schema["properties"]
        self.assertEqual("string", properties["schema_version"]["type"])
        self.assertEqual(["1.0"], properties["schema_version"]["enum"])
        self.assertEqual("string", properties["outcome"]["type"])
        self.assertEqual(sorted(orchestration.RESULT_OUTCOMES), properties["outcome"]["enum"])
        self.assertTrue(all("type" in definition for definition in properties.values()))
        self.assertEqual(set(properties), set(schema["required"]))
        self.assertFalse(schema["additionalProperties"])
        for unsupported in ("minLength", "maxLength", "minItems", "maxItems", "uniqueItems"):
            self.assertNotIn(f'"{unsupported}"', json.dumps(schema))
        self.assertEqual({"type": "string"}, properties["artifacts"]["items"])

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
        self.assertEqual(
            sys.executable,
            execute.call_args.kwargs["environment"][orchestration.MANAGED_PYTHON_ENV],
        )
        self.assertEqual("/tmp/fake claude", argv[0])
        self.assertIn("--json-schema", argv)
        self.assertEqual(
            orchestration.ORCHESTRATION_RESULT_SCHEMA,
            json.loads(argv[argv.index("--json-schema") + 1]),
        )

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
                    runtime_read_paths=("/opt/codex-runtime",),
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
                    runtime_read_paths=("/opt/codex-runtime",),
                )

    def test_host_staged_result_is_bound_to_work_order_identity_and_lease_attempt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            attempt = orchestration._start_attempt(root, order, "codex")
            path = orchestration._stage_host_result(
                root,
                order,
                result(),
                attempt_id=attempt["attempt_id"],
            )
            envelope = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual("orchestration_host_staged_result", envelope["artifact_type"])
            self.assertEqual(1, envelope["lease_attempt"])
            self.assertEqual(orchestration._work_order_identity(order), envelope["work_order_identity"])
            self.assertNotIn(str(root), path.read_text(encoding="utf-8"))
            self.assertEqual(result(), orchestration._load_host_staged_result(root, order))

            replayed = json.loads(json.dumps(order))
            replayed["issued_at"] = "2026-07-20T00:02:00Z"
            replayed["lease"]["attempt"] = 2
            replayed["lease"]["expires_at"] = "2026-07-20T00:03:00Z"
            self.assertEqual(result(), orchestration._load_host_staged_result(root, replayed))

            conflicting = json.loads(json.dumps(replayed))
            conflicting["scope"]["question_slugs"] = ["different-question"]
            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "does not match the replayed work order",
            ):
                orchestration._load_host_staged_result(root, conflicting)

    def test_staging_rejects_attempt_id_that_conflicts_with_its_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            attempt = orchestration._start_attempt(root, order, "codex")
            path = orchestration._attempt_path(root, "orch-1", attempt["attempt_id"])
            corrupted = json.loads(path.read_text(encoding="utf-8"))
            corrupted["attempt_id"] = "attempt-different"
            path.write_text(json.dumps(corrupted), encoding="utf-8")

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "does not match the result work order",
            ):
                orchestration._stage_host_result(
                    root,
                    order,
                    result(),
                    attempt_id=attempt["attempt_id"],
                )

    def test_host_staged_envelope_recovers_a_near_limit_valid_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            attempt = orchestration._start_attempt(root, order, "codex")
            near_limit = {
                "schema_version": "1.0",
                "action_id": order["action_id"],
                "outcome": "completed",
                "summary": "s" * 4000,
                "artifacts": [f"wiki/{index:03d}-{'x' * 224}.md" for index in range(256)],
            }
            self.assertLessEqual(
                len(json.dumps(near_limit, separators=(",", ":")).encode("utf-8")),
                orchestration.MAX_RESULT_BYTES,
            )

            staged_path = orchestration._stage_host_result(
                root,
                order,
                near_limit,
                attempt_id=attempt["attempt_id"],
            )

            self.assertGreater(staged_path.stat().st_size, orchestration.MAX_RESULT_BYTES)
            self.assertEqual(near_limit, orchestration._load_host_staged_result(root, order))

    def test_attempt_contract_is_bounded_and_contains_no_prompt_or_path_fields(self):
        schema = orchestration.ORCHESTRATION_ATTEMPT_SCHEMA
        self.assertEqual("object", schema["type"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(orchestration.ATTEMPT_KEYS), set(schema["required"]))
        self.assertEqual(sorted(orchestration.ATTEMPT_STATUSES), schema["properties"]["status"]["enum"])
        for forbidden in ("prompt", "transcript", "diagnostic", "absolute_path", "model_output"):
            self.assertNotIn(forbidden, schema["properties"])

    def test_private_atomic_replace_retries_windows_sharing_violation(self):
        sharing_error = OSError("sharing violation")
        sharing_error.winerror = 32  # type: ignore[attr-defined]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            temporary = root / "temporary.json"
            destination = root / "destination.json"
            temporary.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(orchestration.os, "name", "nt"), mock.patch.object(
                orchestration.os,
                "replace",
                side_effect=[sharing_error, None],
            ) as replace, mock.patch.object(orchestration.time, "sleep") as sleep:
                orchestration._replace_private_file(temporary, destination)

            self.assertEqual(2, replace.call_count)
            sleep.assert_called_once_with(orchestration.WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[0])

    def test_private_atomic_write_wraps_temporary_file_creation_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            orchestration.tempfile,
            "mkstemp",
            side_effect=PermissionError("denied"),
        ), self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            "Could not create a private temporary file",
        ):
            orchestration._write_private_json_atomic(Path(tmpdir) / "state.json", {"ok": True})

    def test_attempt_id_is_independent_of_the_maximum_length_action_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order("a" * 200)

            attempt = orchestration._start_attempt(root, order, "codex")

            self.assertRegex(attempt["attempt_id"], r"^attempt-[0-9a-f]{32}$")
            self.assertLessEqual(len(attempt["attempt_id"]), 200)

    def test_drive_session_submits_recovered_host_stage_without_rerunning_worker(self):
        terminal = {"orchestration_id": "orch-1", "status": "complete", "verdict": "complete"}
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            attempt = orchestration._start_attempt(root, order, "codex")
            staged_path = orchestration._stage_host_result(
                root,
                order,
                result(),
                attempt_id=attempt["attempt_id"],
            )
            orchestration._update_attempt(root, attempt, status_value="result_staged", result=result())

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return order
                raise AssertionError(command)

            with mock.patch.object(
                orchestration, "_runner_executable", side_effect=AssertionError("runner must not be resolved")
            ) as resolve_runner, mock.patch.object(
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "execute_work_order"
            ) as execute, mock.patch.object(
                orchestration, "_submit_result", return_value=terminal
            ) as submit:
                final = orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            self.assertEqual(terminal, final)
            resolve_runner.assert_not_called()
            execute.assert_not_called()
            submit.assert_called_once_with(root, "orch-1", "agent-1", result())
            self.assertFalse(staged_path.exists())
            retained_attempt = orchestration._load_attempt(
                orchestration._attempt_path(root, "orch-1", attempt["attempt_id"])
            )
            self.assertEqual("submitted", retained_attempt["status"])

    def test_drive_session_rejects_staged_result_from_terminal_error_attempt(self):
        states = {
            "runner_failed": "RUNNER_FAILED",
            "timed_out": "RUNNER_TIMEOUT",
            "interrupted": "RUNNER_INTERRUPTED",
            "control_tampered": "CONTROL_ARTIFACT_TAMPERED",
            "repair_acknowledged": "CONTROL_ARTIFACT_TAMPERED",
        }
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}

        for status_value, error_code in states.items():
            with self.subTest(status=status_value), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                control_workspace(root)
                order = work_order()
                attempt = orchestration._start_attempt(root, order, "codex")
                orchestration._stage_host_result(
                    root,
                    order,
                    result(),
                    attempt_id=attempt["attempt_id"],
                )
                orchestration._update_attempt(
                    root,
                    attempt,
                    status_value=status_value,
                    result=result(),
                    error_code=error_code,
                )

                def controller(_root, command, _arguments, *, order=order):
                    if command == "status":
                        return active
                    if command == "next":
                        return order
                    raise AssertionError(command)

                with mock.patch.object(
                    orchestration,
                    "_controller_json",
                    side_effect=controller,
                ), mock.patch.object(
                    orchestration,
                    "execute_work_order",
                ) as execute, mock.patch.object(
                    orchestration,
                    "_submit_result",
                ) as submit, self.assertRaisesRegex(
                    orchestration.OrchestrationHostError,
                    "cannot resume from a staged result",
                ):
                    orchestration.drive_session(
                        root,
                        "orch-1",
                        runner="codex",
                        agent_id="agent-1",
                        model=None,
                        action_timeout_seconds=60,
                    )

                execute.assert_not_called()
                submit.assert_not_called()

    def test_drive_session_retains_host_stage_when_submission_fails(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return order
                raise AssertionError(command)

            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "execute_work_order", return_value=result()
            ), mock.patch.object(
                orchestration,
                "_submit_result",
                side_effect=orchestration.OrchestrationHostError("injected submit crash"),
            ), self.assertRaisesRegex(orchestration.OrchestrationHostError, "injected submit crash"):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            staged = orchestration._load_host_staged_result(root, order)
            self.assertEqual(result(), staged)
            attempts = [
                orchestration._load_attempt(path)
                for path in (root / "runs" / "orchestrations" / "orch-1" / "attempts").glob("*.json")
            ]
            self.assertEqual(["result_staged"], [attempt["status"] for attempt in attempts])

    def test_drive_session_records_bounded_runner_failure_without_a_result(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return work_order()
                raise AssertionError(command)

            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration,
                "execute_work_order",
                side_effect=orchestration.OrchestrationHostError(
                    "Managed codex action exited with code 9; the action remains resumable.",
                    exit_code=orchestration.EXIT_RUNNER_FAILED,
                ),
            ), self.assertRaisesRegex(orchestration.OrchestrationHostError, "remains resumable"):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            attempts = [
                orchestration._load_attempt(path)
                for path in (root / "runs" / "orchestrations" / "orch-1" / "attempts").glob("*.json")
            ]
            self.assertEqual(1, len(attempts))
            self.assertEqual("runner_failed", attempts[0]["status"])
            self.assertEqual("RUNNER_FAILED", attempts[0]["error_code"])
            self.assertIsNone(attempts[0]["result_digest"])
            serialized = json.dumps(attempts[0])
            self.assertNotIn(str(root), serialized)
            self.assertNotIn("Managed codex action exited", serialized)

    def test_resume_retries_failed_action_and_canonicalizes_managed_artifacts(self):
        terminal = {"orchestration_id": "orch-1", "status": "complete", "verdict": "complete"}
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            original = work_order()
            failed_attempt = orchestration._start_attempt(root, original, "codex")
            orchestration._update_attempt(
                root,
                failed_attempt,
                status_value="runner_failed",
                error_code="RUNNER_FAILED",
            )
            replayed = json.loads(json.dumps(original))
            replayed["issued_at"] = "2026-07-20T00:02:00Z"
            replayed["lease"]["attempt"] = 2

            def controller(_root, command, arguments):
                if command == "status":
                    return active
                if command == "next":
                    self.assertIn("--resume", arguments)
                    return replayed
                raise AssertionError(command)

            def fake_execute(argv, **_kwargs):
                output_path = Path(argv[argv.index("--output-last-message") + 1])
                document = result()
                document["artifacts"] = ["runs/orchestrations/orch-1/session.json"]
                output_path.write_text(json.dumps(document), encoding="utf-8")
                return orchestration.ProcessResult(0, "", "")

            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration,
                "_validate_runner_capability",
                return_value=("/opt/codex-runtime",),
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "_execute_bounded", side_effect=fake_execute
            ), mock.patch.object(
                orchestration, "_submit_result", return_value=terminal
            ) as submit:
                final = orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model="gpt-test",
                    action_timeout_seconds=60,
                    resume=True,
                )

            self.assertEqual(terminal, final)
            submitted_result = submit.call_args.args[3]
            self.assertEqual([], submitted_result["artifacts"])
            attempts = [
                orchestration._load_attempt(path)
                for path in (root / "runs" / "orchestrations" / "orch-1" / "attempts").glob("*.json")
            ]
            self.assertCountEqual(["runner_failed", "submitted"], [attempt["status"] for attempt in attempts])

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
        self.assertIn("RUNNER_ISOLATION_UNAVAILABLE", stderr.getvalue())
        self.assertIn("not found on PATH", stderr.getvalue())

    def test_isolation_capability_failure_precedes_session_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration,
                "_validate_runner_capability",
                side_effect=orchestration._runner_isolation_error("injected capability failure"),
            ), mock.patch.object(
                orchestration, "_controller_json"
            ) as controller, contextlib.redirect_stderr(stderr):
                code = orchestration.main(["run", "--target", str(root), "--runner", "codex"])

        self.assertEqual(orchestration.EXIT_RUNNER_FAILED, code)
        controller.assert_not_called()
        self.assertIn("RUNNER_ISOLATION_UNAVAILABLE: injected capability failure", stderr.getvalue())

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
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "_managed_session_lock", return_value=contextlib.nullcontext()
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
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "_managed_session_lock", return_value=contextlib.nullcontext()
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

    def test_resume_enforces_repair_gate_before_reading_parent_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("project: {}\n", encoding="utf-8")
            stderr = io.StringIO()
            with mock.patch.object(
                orchestration,
                "_managed_session_lock",
                return_value=contextlib.nullcontext(),
            ), mock.patch.object(
                orchestration,
                "_control_repair_gate",
                side_effect=orchestration.OrchestrationHostError(
                    "CONTROL_REPAIR_REQUIRED: inspect retained state",
                    exit_code=orchestration.EXIT_RUNNER_FAILED,
                ),
            ) as repair_gate, mock.patch.object(
                orchestration,
                "_controller_json",
            ) as controller, mock.patch.object(
                orchestration,
                "_runner_executable",
            ) as runner, contextlib.redirect_stderr(stderr):
                code = orchestration.main(
                    [
                        "resume",
                        "--target",
                        str(root),
                        "--orchestration-id",
                        "orch-1",
                        "--runner",
                        "codex",
                    ]
                )

        self.assertEqual(orchestration.EXIT_RUNNER_FAILED, code)
        repair_gate.assert_called_once_with(root.resolve(), "orch-1", acknowledge=False)
        controller.assert_not_called()
        runner.assert_not_called()
        self.assertIn("CONTROL_REPAIR_REQUIRED", stderr.getvalue())

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
        attempt = {"attempt_id": "action-0001-attempt-test"}
        with mock.patch.object(orchestration, "_runner_executable", return_value="/tmp/fake codex"), mock.patch.object(
            orchestration, "_validate_runner_capability"
        ), mock.patch.object(
            orchestration, "_controller_json", side_effect=controller
        ), mock.patch.object(orchestration, "execute_work_order", return_value=result()), mock.patch.object(
            orchestration, "_submit_result", return_value=paused
        ), mock.patch.object(
            orchestration, "_stage_host_result"
        ), mock.patch.object(
            orchestration, "_discard_host_staged_result"
        ), mock.patch.object(
            orchestration, "_capture_control_artifacts", return_value=snapshot
        ), mock.patch.object(
            orchestration, "_verify_control_artifacts_unchanged"
        ), mock.patch.object(
            orchestration, "_start_attempt", return_value=attempt
        ), mock.patch.object(
            orchestration, "_update_attempt", return_value=attempt
        ), mock.patch.object(
            orchestration, "_managed_session_lock", return_value=contextlib.nullcontext()
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

    def test_runner_control_tampering_is_not_submitted_and_changes_are_left_for_inspection(self):
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
                    orchestration, "_validate_runner_capability"
                ), mock.patch.object(
                    orchestration, "_controller_json", side_effect=controller
                ), mock.patch.object(
                    orchestration, "execute_work_order", side_effect=mutate_then_complete
                ), mock.patch.object(orchestration, "_submit_result") as submit:
                    with self.assertRaisesRegex(
                        orchestration.OrchestrationHostError,
                        "CONTROL_ARTIFACT_TAMPERED.*did not roll back.*operator inspection",
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
                attempt_paths = sorted((parent / "attempts").glob("*.json"))
                self.assertEqual(1, len(attempt_paths))
                attempt_record = orchestration._load_attempt(attempt_paths[0])
                self.assertEqual("control_tampered", attempt_record["status"])
                self.assertEqual("CONTROL_ARTIFACT_TAMPERED", attempt_record["error_code"])
                self.assertEqual(orchestration._document_digest(result()), attempt_record["result_digest"])
                quarantine_paths = sorted((parent / "quarantine").glob("*.json"))
                self.assertEqual(1, len(quarantine_paths))
                quarantined = json.loads(quarantine_paths[0].read_text(encoding="utf-8"))
                self.assertEqual("orchestration_quarantined_result", quarantined["artifact_type"])
                self.assertEqual("CONTROL_ARTIFACT_TAMPERED", quarantined["reason_code"])
                self.assertEqual(result(), quarantined["result"])
                self.assertNotIn(str(root), json.dumps(quarantined))
                self.assertIn("validated result was quarantined", str(raised.exception))
                repair_marker = orchestration._load_control_repair(root, "orch-1")
                self.assertIsNotNone(repair_marker)
                assert repair_marker is not None
                self.assertEqual("required", repair_marker["status"])
                self.assertEqual([attempt_record["attempt_id"]], repair_marker["attempt_ids"])
                with self.assertRaisesRegex(
                    orchestration.OrchestrationHostError,
                    "CONTROL_REPAIR_REQUIRED",
                ):
                    orchestration._control_repair_gate(root, "orch-1", acknowledge=False)
                with self.assertRaisesRegex(
                    orchestration.OrchestrationHostError,
                    "CONTROL_REPAIR_MISMATCH",
                ):
                    orchestration._control_repair_gate(root, "orch-1", acknowledge=True)
                if mutation_name == "config":
                    after_parent = file_tree(parent)
                    for relative, content in before_parent.items():
                        self.assertEqual(content, after_parent[relative])
                    self.assertIn("attacker", (root / "research.yml").read_text(encoding="utf-8"))
                    self.assertIn("research.yml [content_changed]", str(raised.exception))
                else:
                    self.assertNotEqual(before_parent, file_tree(parent))
                    expected_path = {
                        "session": "runs/orchestrations/orch-1/session.json [content_changed]",
                        "work-order": "runs/orchestrations/orch-1/work-orders/action-0001.json [content_changed]",
                        "preseed-result": "runs/orchestrations/orch-1/work-results/action-0001.json [added]",
                    }[mutation_name]
                    self.assertIn(expected_path, str(raised.exception))

    def test_preexisting_unsafe_control_tree_does_not_leave_a_running_attempt(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)
            (root / "scripts" / "unsafe.py").symlink_to(root / "research.yml")

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return work_order()
                raise AssertionError(command)

            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "execute_work_order"
            ) as execute, self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_UNSAFE.*symbolic",
            ):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            execute.assert_not_called()
            self.assertFalse((parent / "attempts").exists())

    def test_control_tamper_requires_explicit_repair_acknowledgement_before_resume(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            order = work_order()
            attempt = orchestration._start_attempt(root, order, "codex")
            control_snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            attempt = orchestration._update_attempt(
                root,
                attempt,
                status_value="control_tampered",
                result=result(),
                error_code="CONTROL_ARTIFACT_TAMPERED",
            )
            orchestration._mark_control_repair_required(
                root,
                "orch-1",
                attempt["attempt_id"],
                control_snapshot,
            )

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_REPAIR_REQUIRED.*--acknowledge-control-repair",
            ):
                orchestration._control_repair_gate(root, "orch-1", acknowledge=False)

            retained = orchestration._load_attempt(
                orchestration._attempt_path(root, "orch-1", attempt["attempt_id"])
            )
            self.assertEqual("control_tampered", retained["status"])

            orchestration._control_repair_gate(root, "orch-1", acknowledge=True)

            retained = orchestration._load_attempt(
                orchestration._attempt_path(root, "orch-1", attempt["attempt_id"])
            )
            self.assertEqual("repair_acknowledged", retained["status"])
            self.assertEqual("CONTROL_ARTIFACT_TAMPERED", retained["error_code"])
            self.assertEqual(orchestration._document_digest(result()), retained["result_digest"])

    def test_control_repair_refuses_acknowledgement_without_pre_action_baseline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            attempt = orchestration._start_attempt(root, work_order(), "codex")
            orchestration._update_attempt(
                root,
                attempt,
                status_value="control_tampered",
                result=result(),
                error_code="CONTROL_ARTIFACT_TAMPERED",
            )

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_REPAIR_BASELINE_MISSING",
            ):
                orchestration._control_repair_gate(root, "orch-1", acknowledge=True)

    def test_durable_repair_marker_survives_deleted_attempt_record(self):
        active = {"orchestration_id": "orch-1", "status": "active", "phase": "research"}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)

            def controller(_root, command, _arguments):
                if command == "status":
                    return active
                if command == "next":
                    return work_order()
                raise AssertionError(command)

            def delete_attempt_then_complete(*_args, **_kwargs):
                attempts = parent / "attempts"
                for path in attempts.glob("*.json"):
                    path.unlink()
                attempts.rmdir()
                return result()

            with mock.patch.object(
                orchestration, "_runner_executable", return_value="/tmp/fake codex"
            ), mock.patch.object(
                orchestration, "_validate_runner_capability"
            ), mock.patch.object(
                orchestration, "_controller_json", side_effect=controller
            ), mock.patch.object(
                orchestration, "execute_work_order", side_effect=delete_attempt_then_complete
            ), mock.patch.object(
                orchestration, "_submit_result"
            ) as submit, self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_TAMPERED",
            ):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                )

            submit.assert_not_called()
            self.assertFalse((parent / "attempts").exists())
            marker = orchestration._load_control_repair(root, "orch-1")
            self.assertIsNotNone(marker)
            assert marker is not None
            self.assertEqual("required", marker["status"])
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "CONTROL_REPAIR_REQUIRED"):
                orchestration._control_repair_gate(root, "orch-1", acknowledge=False)

    def test_durable_repair_marker_survives_parent_tree_replacement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)
            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            attempt = orchestration._start_attempt(root, work_order(), "codex")
            marker_path = orchestration._write_control_repair(
                root,
                {
                    "schema_version": "1.0",
                    "artifact_type": "orchestration_control_repair",
                    "orchestration_id": "orch-1",
                    "status": "required",
                    "reason_code": "CONTROL_ARTIFACT_TAMPERED",
                    "detected_at": "2026-07-21T00:00:00Z",
                    "acknowledged_at": None,
                    "attempt_ids": [attempt["attempt_id"]],
                    "expected_control_fingerprint": orchestration._tripwire_control_fingerprint(snapshot),
                },
            )
            displaced = root / "runs" / "displaced-orchestration"
            parent.rename(displaced)
            parent.mkdir()

            self.assertEqual(root / "runs" / "orchestration-guards" / "orch-1.json", marker_path)
            self.assertTrue(marker_path.is_file())
            marker = orchestration._load_control_repair(root, "orch-1")
            self.assertIsNotNone(marker)
            with self.assertRaisesRegex(orchestration.OrchestrationHostError, "CONTROL_REPAIR_REQUIRED"):
                orchestration._control_repair_gate(root, "orch-1", acknowledge=False)

    def test_managed_session_lock_refuses_a_second_host_before_controller_or_runner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            with orchestration._managed_session_lock(root, "orch-1"), mock.patch.object(
                orchestration, "_controller_json"
            ) as controller, mock.patch.object(
                orchestration, "_runner_executable"
            ) as runner, self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "ORCHESTRATION_ALREADY_RUNNING",
            ):
                orchestration.drive_session(
                    root,
                    "orch-1",
                    runner="codex",
                    agent_id="agent-1",
                    model=None,
                    action_timeout_seconds=60,
                    resume=True,
                )

            controller.assert_not_called()
            runner.assert_not_called()

    def test_control_snapshot_excludes_only_the_held_managed_host_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)
            with orchestration._managed_session_lock(root, "orch-1"):
                snapshot = orchestration._capture_control_artifacts(root, "orch-1")
                parent_entries = snapshot.roots["runs/orchestrations/orch-1"]
                self.assertIn(".locks", parent_entries)
                self.assertNotIn(orchestration.MANAGED_HOST_LOCK_CONTROL_PATH, parent_entries)

                orchestration._verify_control_artifacts_unchanged(root, snapshot)

                (parent / ".locks" / "unexpected.lock").write_text("unexpected\n", encoding="utf-8")
                with self.assertRaisesRegex(
                    orchestration.OrchestrationHostError,
                    r"CONTROL_ARTIFACT_TAMPERED.*\.locks/unexpected\.lock \[added\]",
                ):
                    orchestration._verify_control_artifacts_unchanged(root, snapshot)

    def test_control_snapshot_ignores_mtime_only_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            parent = control_workspace(root)
            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            session_path = parent / "session.json"
            timestamps = (session_path.stat().st_atime_ns, session_path.stat().st_mtime_ns + 1_000_000)
            os.utime(session_path, ns=timestamps)

            orchestration._verify_control_artifacts_unchanged(root, snapshot)

    def test_repair_fingerprint_covers_static_controls_but_ignores_host_runtime_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            baseline = orchestration._capture_control_artifacts(root, "orch-1")
            fingerprint = orchestration._tripwire_control_fingerprint(baseline)

            attempt = orchestration._start_attempt(root, work_order(), "codex")
            with_runtime_record = orchestration._capture_control_artifacts(root, "orch-1")
            self.assertEqual(
                fingerprint,
                orchestration._tripwire_control_fingerprint(with_runtime_record),
            )

            config = root / "research.yml"
            config.write_text("project: {name: changed}\n", encoding="utf-8")
            changed = orchestration._capture_control_artifacts(root, "orch-1")
            self.assertNotEqual(
                fingerprint,
                orchestration._tripwire_control_fingerprint(changed),
            )
            self.assertEqual("running", attempt["status"])

    def test_control_diff_uses_reason_codes_and_bounds_reported_paths(self):
        before_entry = orchestration.ControlArtifactEntry("file", 0o600, 1, "old")
        after_entry = orchestration.ControlArtifactEntry("directory", 0o700, 0, None)
        before = orchestration.ControlArtifactSnapshot("orch-1", {"control": {"entry": before_entry}}, 1)
        after = orchestration.ControlArtifactSnapshot("orch-1", {"control": {"entry": after_entry}}, 0)
        self.assertEqual(
            ["control/entry [kind_changed:file->directory,mode_changed:600->700,content_changed]"],
            orchestration._control_artifact_differences(before, after),
        )

        missing_entry = orchestration.ControlArtifactEntry("missing", 0, 0, None)
        file_entry = orchestration.ControlArtifactEntry("file", 0o600, 1, "digest")
        self.assertEqual(
            ["control [added]"],
            orchestration._control_artifact_differences(
                orchestration.ControlArtifactSnapshot("orch-1", {"control": {"": missing_entry}}, 0),
                orchestration.ControlArtifactSnapshot("orch-1", {"control": {"": file_entry}}, 1),
            ),
        )
        self.assertEqual(
            ["control [removed]"],
            orchestration._control_artifact_differences(
                orchestration.ControlArtifactSnapshot("orch-1", {"control": {"": file_entry}}, 1),
                orchestration.ControlArtifactSnapshot("orch-1", {"control": {"": missing_entry}}, 0),
            ),
        )

        many_after = orchestration.ControlArtifactSnapshot(
            "orch-1",
            {
                "control": {
                    f"entry-{index}": orchestration.ControlArtifactEntry("file", 0o600, 1, str(index))
                    for index in range(orchestration.MAX_CONTROL_DIFFS_REPORTED + 3)
                }
            },
            0,
        )
        empty_before = orchestration.ControlArtifactSnapshot("orch-1", {"control": {}}, 0)
        with mock.patch.object(
            orchestration,
            "_capture_current_control_artifacts",
            return_value=many_after,
        ), self.assertRaisesRegex(
            orchestration.OrchestrationHostError,
            r"\[3 additional_paths_omitted\]",
        ):
            orchestration._verify_control_artifacts_unchanged(REPO_ROOT, empty_before)

    def test_run_report_carveout_is_writable_but_other_docs_remain_protected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            settings = orchestration._claude_host_settings(root, allow_network=False)
            deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
            allow_write = settings["sandbox"]["filesystem"]["allowWrite"]
            permission_deny = settings["permissions"]["deny"]

            self.assertIn(str(root / "docs"), deny_write)
            self.assertNotIn(str(root / "runs/run-reports"), deny_write)
            self.assertIn(str(root / "runs/run-reports"), allow_write)
            for tool in ("Edit", "Write"):
                self.assertIn(f"{tool}(/docs)", permission_deny)
                self.assertIn(f"{tool}(/docs/**)", permission_deny)
                self.assertNotIn(f"{tool}(/runs/run-reports)", permission_deny)

            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            (root / "runs" / "run-reports" / "run-1.md").write_text("# Report\n", encoding="utf-8")
            orchestration._verify_control_artifacts_unchanged(root, snapshot)

            (root / "docs" / "contract.md").write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                r"docs/contract\.md \[content_changed\]",
            ):
                orchestration._verify_control_artifacts_unchanged(root, snapshot)

    def test_control_snapshot_rejects_symlink_in_writable_run_report_carveout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            (root / "runs" / "run-reports" / "linked.md").symlink_to(root / "research.yml")

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_UNSAFE.*writable control carveout.*symbolic",
            ):
                orchestration._capture_control_artifacts(root, "orch-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            (root / "runs" / "run-reports" / "linked.md").symlink_to(root / "research.yml")

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_TAMPERED.*inspection_failed.*writable control carveout.*symbolic",
            ):
                orchestration._verify_control_artifacts_unchanged(root, snapshot)

    def test_control_snapshot_rejects_hardlink_aliases_to_protected_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            alias = root / "runs" / "run-reports" / "research-alias.yml"
            try:
                os.link(root / "research.yml", alias)
            except OSError as exc:  # pragma: no cover - filesystem capability guard
                self.skipTest(f"hard links are unavailable on this filesystem: {exc}")

            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_UNSAFE.*(?:hard links|multiply linked)",
            ):
                orchestration._capture_control_artifacts(root, "orch-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            control_workspace(root)
            snapshot = orchestration._capture_control_artifacts(root, "orch-1")
            os.link(root / "research.yml", root / "runs" / "run-reports" / "research-alias.yml")
            with self.assertRaisesRegex(
                orchestration.OrchestrationHostError,
                "CONTROL_ARTIFACT_TAMPERED.*inspection_failed.*(?:hard links|multiply linked)",
            ):
                orchestration._verify_control_artifacts_unchanged(root, snapshot)

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
