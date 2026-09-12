import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli  # noqa: E402

INIT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "init_research_workspace.py"
TEMPLATE_QUERY_INDEX = REPO_ROOT / "workspace-template" / "scripts" / "query_index.py"
TEMPLATE_INTAKE = REPO_ROOT / "workspace-template" / "scripts" / "intake_questions.py"
TEMPLATE_INIT = REPO_ROOT / "workspace-template" / "scripts" / "init_research_workspace.py"


INIT = load_script_module("research_init_for_upgrade", INIT_PATH)


CONTROLLER_PATH = REPO_ROOT / "workspace-template" / "scripts" / "orchestration_controller.py"
CONTROLLER = load_script_module("research_controller_for_upgrade", CONTROLLER_PATH)

HOLDING_LOCK = """
import importlib.util, os, sys, time
from pathlib import Path

scripts, lock_path, ready, release = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("held_workspace_locks", str(Path(scripts) / "_workspace_locks.py"))
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

with module.workspace_lock(Path(lock_path), timeout_seconds=0.0, purpose="test holder"):
    Path(ready).write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not Path(release).exists():
        time.sleep(0.02)
"""


def controller(target: Path, *args: str) -> tuple[int, dict, str]:
    process = subprocess.run(  # noqa: S603
        [sys.executable, "-B", str(CONTROLLER_PATH), "--project-root", str(target), *args, "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = process.stdout.strip() or process.stderr.strip()
    return process.returncode, json.loads(text) if text else {}, process.stderr


def issue_pending_order(target: Path, orchestration_id: str = "orch-upgrade") -> dict:
    """Drive a real session to a pending work order through the controller."""
    code, _, stderr = controller(target, "start", "--orchestration-id", orchestration_id, "--agent-id", "agent-a")
    assert code == 0, stderr
    code, order, stderr = controller(target, "next", "--orchestration-id", orchestration_id, "--agent-id", "agent-a")
    assert code == 0, stderr
    return order


def run_cli(*args: str) -> tuple[int, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = cli.main(list(args))
    return code, stdout.getvalue()


def run_cli_result(*args: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(list(args))
    return int(code or 0), stdout.getvalue(), stderr.getvalue()


def init_workspace(target: Path) -> Path:
    run_cli(
        "init",
        "--target",
        str(target),
        "--project-name",
        "upgrade-workspace",
        "--project-description",
        "Workspace used to exercise the upgrade command.",
    )
    return target


class UpgradeCliTests(unittest.TestCase):
    def test_help_mentions_upgrade(self):
        _code, output = run_cli("--help")
        self.assertIn("upgrade", output)

    def test_help_mentions_force_optional_upgrade(self):
        _code, output = run_cli("--help")
        self.assertIn("--force-optional", output)

    def test_both_help_surfaces_truthfully_describe_upgrade_locks_and_log_append(self):
        code, root_help = run_cli("--help")
        self.assertEqual(0, code)
        upgrade_stdout = io.StringIO()
        with contextlib.redirect_stdout(upgrade_stdout):
            with self.assertRaises(SystemExit) as exited:
                cli.main(["upgrade", "--help"])
        self.assertEqual(0, exited.exception.code)
        for output in (root_help, upgrade_stdout.getvalue()):
            self.assertIn(".locks/", output)
            self.assertIn("log.md", output)
            self.assertIn("dry-run writes nothing", output)
            self.assertNotIn("or log.md", output)

    def test_upgrade_refreshes_drifted_script(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            drifted = target / "scripts" / "query_index.py"
            drifted.write_text("# stale local copy\n")

            code, output = run_cli("upgrade", "--target", str(target))

            self.assertEqual(code, 0)
            self.assertIn("Upgraded research workspace", output)
            self.assertIn("scripts/query_index.py", output)
            self.assertEqual(drifted.read_bytes(), TEMPLATE_QUERY_INDEX.read_bytes())
            self.assertIn("] upgrade |", (target / "log.md").read_text())
            self.assertTrue((target / ".locks").is_dir())

    def test_upgrade_refreshes_metadata_intake_siblings_and_accepts_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = init_workspace(root / "workspace")
            intake = target / "scripts" / "intake_questions.py"
            initializer = target / "scripts" / "init_research_workspace.py"
            intake.write_text("# stale intake\n", encoding="utf-8")
            initializer.write_text("# stale initializer\n", encoding="utf-8")

            code, output = run_cli("upgrade", "--target", str(target))

            self.assertEqual(0, code)
            self.assertIn("scripts/intake_questions.py", output)
            self.assertIn("scripts/init_research_workspace.py", output)
            self.assertEqual(TEMPLATE_INTAKE.read_bytes(), intake.read_bytes())
            self.assertEqual(TEMPLATE_INIT.read_bytes(), initializer.read_bytes())

            batch = root / "metadata-batch.json"
            batch.write_text(
                '{"schema_version":"1.0","questions":[{"question":"Upgraded metadata?",'
                '"metadata":{"candidate_id":"upgrade-1"}}]}',
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(intake),
                    "--project-root",
                    str(target),
                    "--from-file",
                    str(batch),
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            page = target / "wiki" / "questions" / "upgraded-metadata.md"
            frontmatter = yaml.safe_load(page.read_text(encoding="utf-8").split("---\n", 2)[1])
            self.assertEqual({"candidate_id": "upgrade-1"}, frontmatter["metadata"])

            invalid_batch = root / "invalid-metadata-batch.json"
            invalid_batch.write_text(
                '{"schema_version":"1.0","questions":[{"question":"Invalid upgraded metadata?",'
                '"metadata":{"candidate_ids":["upgrade-1"]}}]}',
                encoding="utf-8",
            )
            refused = subprocess.run(
                [
                    sys.executable,
                    str(intake),
                    "--project-root",
                    str(target),
                    "--from-file",
                    str(invalid_batch),
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, refused.returncode)
            self.assertEqual("WORKSPACE_UNREADABLE", json.loads(refused.stderr)["error_code"])
            self.assertNotIn("Traceback", refused.stderr)
            self.assertFalse(
                (target / "wiki" / "questions" / "invalid-upgraded-metadata.md").exists()
            )

    def test_upgrade_noop_when_current(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            log_before = (target / "log.md").read_text()

            code, output = run_cli("upgrade", "--target", str(target))

            self.assertEqual(code, 0)
            self.assertIn("no changes", output)
            # A no-op upgrade should not append an upgrade log entry.
            self.assertEqual((target / "log.md").read_text(), log_before)
            self.assertTrue((target / ".locks").is_dir())

    def test_upgrade_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            drifted = target / "scripts" / "query_index.py"
            drifted.write_text("# stale local copy\n")
            log_before = (target / "log.md").read_text()

            code, output = run_cli("upgrade", "--target", str(target), "--dry-run")

            self.assertEqual(code, 0)
            self.assertIn("would update", output)
            self.assertEqual(drifted.read_text(), "# stale local copy\n")
            self.assertEqual((target / "log.md").read_text(), log_before)
            self.assertFalse((target / ".locks").exists())

    def test_upgrade_preserves_user_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            research_before = (target / "research.yml").read_text()
            custom_page = target / "wiki" / "concepts" / "user-note.md"
            custom_page.write_text("---\ntype: concept\nsource_ids: []\n---\n\n# User Note\n\nKeep me.\n")
            (target / "scripts" / "query_index.py").write_text("# stale\n")

            run_cli("upgrade", "--target", str(target))

            self.assertEqual((target / "research.yml").read_text(), research_before)
            self.assertTrue(custom_page.is_file())
            self.assertIn("Keep me.", custom_page.read_text())

    def test_upgrade_refuses_non_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "not-a-workspace"
            empty.mkdir()
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code, _output = run_cli("upgrade", "--target", str(empty))

            self.assertEqual(2, code)
            self.assertIn("WORKSPACE_UNREADABLE", stderr.getvalue())
            self.assertIn("Remediation:", stderr.getvalue())
            self.assertIn("Preserved:", stderr.getvalue())

    def test_upgrade_write_failure_is_bounded_preserves_state_and_retries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            drifted = target / "scripts" / "query_index.py"
            drifted.write_text("# stale local copy\n", encoding="utf-8")
            before = {
                path.relative_to(target).as_posix(): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            original_write_bytes = Path.write_bytes

            def deny_managed_temp(path: Path, contents: bytes) -> int:
                if path.name == ".query_index.py.tmp":
                    raise PermissionError(13, "Permission denied", str(path))
                return original_write_bytes(path, contents)

            with mock.patch.object(Path, "write_bytes", new=deny_managed_temp):
                code, stdout, stderr = run_cli_result("upgrade", "--target", str(target))

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertIn("UPGRADE_WRITE_FAILED: Could not write starter-managed path scripts/query_index.py", stderr)
            self.assertIn("Remediation:", stderr)
            self.assertIn("Preserved:", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn(str(target), stderr)
            self.assertLess(len(stderr), 800)
            self.assertEqual(
                before,
                {
                    path.relative_to(target).as_posix(): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                },
            )
            self.assertFalse((target / "scripts" / ".query_index.py.tmp").exists())
            self.assertFalse((target / ".locks").exists())

            retry_code, retry_stdout, retry_stderr = run_cli_result("upgrade", "--target", str(target))

            self.assertEqual(0, retry_code, retry_stderr)
            self.assertEqual("", retry_stderr)
            self.assertIn("scripts/query_index.py", retry_stdout)
            self.assertEqual(TEMPLATE_QUERY_INDEX.read_bytes(), drifted.read_bytes())


class UpgradeUnitTests(unittest.TestCase):
    def make_starter(self, root: Path, version: str = "0.9.0") -> Path:
        starter = root / "starter"
        starter.mkdir()
        for name in ("research.yml", "AGENTS.md", "index.md", "log.md"):
            (starter / name).write_text(f"# {name}\n")
        (starter / "workspace-system.yml").write_text(
            "workspace_system:\n"
            f'  starter_version: "{version}"\n'
            '  schema_version: "0.1"\n'
            '  compatible_research_yml_contract: "0.1"\n'
        )
        (starter / "scripts").mkdir()
        (starter / "scripts" / "tool.py").write_text("v2\n")
        (starter / "skills").mkdir()
        (starter / "skills" / "skill.md").write_text("skill v2\n")
        (starter / "docs").mkdir()
        (starter / "docs" / "guide.md").write_text("doc v2\n")
        return starter

    def make_workspace(self, root: Path, version: str = "0.1.0") -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "research.yml").write_text("project:\n  name: t\n")
        (workspace / "workspace-system.yml").write_text(
            "# metadata\n"
            "workspace_system:\n"
            f'  starter_version: "{version}"\n'
            '  schema_version: "0.1"\n'
            '  compatible_research_yml_contract: "0.1"\n'
        )
        (workspace / "log.md").write_text("# Research Wiki Activity Log\n\n")
        (workspace / "scripts").mkdir()
        (workspace / "scripts" / "tool.py").write_text("v1\n")
        return workspace

    def test_scripts_refreshed_and_version_synced(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)

            result = INIT.upgrade_workspace(starter, workspace, ["scripts"], dry_run=False)

            self.assertEqual((workspace / "scripts" / "tool.py").read_text(), "v2\n")
            self.assertEqual(result["updated"], ["scripts/tool.py"])
            self.assertEqual(result["starter_version"], "0.9.0")
            meta = (workspace / "workspace-system.yml").read_text()
            self.assertIn('starter_version: "0.9.0"', meta)
            # Targeted replacement preserves surrounding comments.
            self.assertIn("# metadata", meta)
            self.assertIn("] upgrade |", (workspace / "log.md").read_text())

    def test_include_only_named_optional_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)

            paths = INIT.managed_upgrade_paths(["skills"])
            INIT.upgrade_workspace(starter, workspace, paths, dry_run=False)

            self.assertTrue((workspace / "skills" / "skill.md").is_file())
            self.assertFalse((workspace / "docs" / "guide.md").exists())

    def test_include_docs_refuses_modified_optional_file_without_partial_upgrade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)
            (workspace / "scripts" / "tool.py").write_text("local script drift\n")
            (workspace / "docs").mkdir()
            (workspace / "docs" / "guide.md").write_text("user doc edit\n")

            with self.assertRaises(SystemExit) as raised:
                INIT.upgrade_workspace(starter, workspace, ["scripts", "docs"], dry_run=False)

            self.assertIn("Refusing to overwrite user-edited optional file", str(raised.exception))
            self.assertEqual((workspace / "scripts" / "tool.py").read_text(), "local script drift\n")
            self.assertEqual((workspace / "docs" / "guide.md").read_text(), "user doc edit\n")
            self.assertFalse((workspace / ".replaced" / "docs" / "guide.md").exists())

    def test_force_optional_replaces_modified_doc_and_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "guide.md").write_text("user doc edit\n")

            result = INIT.upgrade_workspace(
                starter,
                workspace,
                ["docs"],
                dry_run=False,
                force_optional=True,
            )

            self.assertEqual((workspace / "docs" / "guide.md").read_text(), "doc v2\n")
            self.assertEqual((workspace / ".replaced" / "docs" / "guide.md").read_text(), "user doc edit\n")
            self.assertEqual(result["updated"], ["docs/guide.md"])
            self.assertEqual(result["replaced"], ["docs/guide.md"])
            log_text = (workspace / "log.md").read_text()
            self.assertIn("Replaced optional files: 1 file(s).", log_text)

    def test_force_optional_refuses_existing_replaced_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)
            (workspace / "docs").mkdir()
            (workspace / "docs" / "guide.md").write_text("user doc edit\n")
            backup = workspace / ".replaced" / "docs" / "guide.md"
            backup.parent.mkdir(parents=True)
            backup.write_text("older backup\n")

            with self.assertRaises(SystemExit) as raised:
                INIT.upgrade_workspace(
                    starter,
                    workspace,
                    ["docs"],
                    dry_run=False,
                    force_optional=True,
                )

            self.assertIn("Refusing to overwrite existing optional backup", str(raised.exception))
            self.assertEqual((workspace / "docs" / "guide.md").read_text(), "user doc edit\n")
            self.assertEqual(backup.read_text(), "older backup\n")

    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)

            result = INIT.upgrade_workspace(starter, workspace, ["scripts"], dry_run=True)

            self.assertEqual(result["updated"], ["scripts/tool.py"])
            self.assertEqual((workspace / "scripts" / "tool.py").read_text(), "v1\n")
            self.assertNotIn("] upgrade |", (workspace / "log.md").read_text())

    def test_previous_version_fixture_preserves_evidence_user_files_and_history(self):
        fixture = REPO_ROOT / "tests" / "fixtures" / "versioned-workspaces" / "0.3.0"
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "versioned-workspace"
            shutil.copytree(fixture, workspace)
            preserved_paths = (
                "research.yml",
                "raw/preserved-evidence.txt",
                "docs/user-note.md",
                "custom/unknown-file.txt",
                "scripts/user-extension.py",
            )
            before = {relative: (workspace / relative).read_bytes() for relative in preserved_paths}
            prior_log = (workspace / "log.md").read_text(encoding="utf-8")

            result = INIT.upgrade_workspace(
                REPO_ROOT / "workspace-template",
                workspace,
                ["scripts"],
                dry_run=False,
            )

            self.assertTrue(result["created"] or result["updated"])
            self.assertEqual(before, {relative: (workspace / relative).read_bytes() for relative in preserved_paths})
            log_text = (workspace / "log.md").read_text(encoding="utf-8")
            self.assertIn(prior_log, log_text)
            self.assertIn("] upgrade |", log_text)
            self.assertIn('starter_version: "0.7.0"', (workspace / "workspace-system.yml").read_text())

    def test_unsupported_contract_refuses_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)
            metadata = workspace / "workspace-system.yml"
            metadata.write_text(metadata.read_text().replace('schema_version: "0.1"', 'schema_version: "9.9"'))
            before = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }

            with self.assertRaises(SystemExit) as raised:
                INIT.upgrade_workspace(starter, workspace, ["scripts"], dry_run=False)

            self.assertIn("workspace_system.schema_version", str(raised.exception))
            self.assertIn("9.9", str(raised.exception))
            self.assertEqual(
                before,
                {
                    path.relative_to(workspace).as_posix(): path.read_bytes()
                    for path in workspace.rglob("*")
                    if path.is_file()
                },
            )
            self.assertFalse((workspace / ".locks").exists())

    def test_interrupted_replace_preserves_prior_state_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            starter = self.make_starter(root)
            workspace = self.make_workspace(root)
            original_replace = Path.replace
            interrupted = False

            def fail_first_managed_replace(path: Path, target: Path):
                nonlocal interrupted
                if not interrupted and path.name == ".tool.py.tmp":
                    interrupted = True
                    raise OSError("synthetic interruption before replace")
                return original_replace(path, target)

            with mock.patch.object(Path, "replace", new=fail_first_managed_replace):
                with self.assertRaises(OSError):
                    INIT.upgrade_workspace(starter, workspace, ["scripts"], dry_run=False)

            self.assertEqual("v1\n", (workspace / "scripts" / "tool.py").read_text())
            self.assertFalse((workspace / "scripts" / ".tool.py.tmp").exists())
            self.assertFalse((workspace / ".locks").exists())
            retry = INIT.upgrade_workspace(starter, workspace, ["scripts"], dry_run=False)
            self.assertEqual("v2\n", (workspace / "scripts" / "tool.py").read_text())
            self.assertEqual(["scripts/tool.py"], retry["updated"])

    def test_refuses_to_upgrade_starter_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            starter = self.make_starter(Path(tmpdir))
            # The starter doubles as a workspace (has both marker files), so the
            # refusal must come from the starter-root guard, not the marker check.
            with self.assertRaises(SystemExit):
                INIT.upgrade_workspace(starter, starter, ["scripts"], dry_run=False)


class UpgradePendingOrderTests(unittest.TestCase):
    """The upgrade refuses to replace what a pending order was issued under."""

    @contextlib.contextmanager
    def holding_lock(self, target: Path, lock_path: Path, scratch: Path) -> Iterator[None]:
        """Hold the lock in another process and release it before workspace cleanup."""
        scratch.mkdir(parents=True, exist_ok=True)
        ready = scratch / "ready"
        release = scratch / "release"
        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "-B", "-c", HOLDING_LOCK, str(target / "scripts"), str(lock_path), str(ready), str(release)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def stop() -> None:
            with contextlib.suppress(OSError):
                release.touch()
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
                process.wait(30)

        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not ready.exists():
                if process.poll() is not None:
                    self.fail(f"lock holder exited early: {process.stderr.read()}")
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "lock holder never reported holding the lock")
            yield
        finally:
            stop()
            process.stdout.close()
            process.stderr.close()

    def test_upgrade_refuses_while_an_orchestration_order_is_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            order = issue_pending_order(target)
            drifted = target / "scripts" / "query_index.py"
            drifted.write_text("# stale local copy\n")
            log_before = (target / "log.md").read_text()
            session_path = target / "runs" / "orchestrations" / "orch-upgrade" / "session.json"
            session_before = session_path.read_bytes()

            for mode in ("write", "dry-run"):
                with self.subTest(mode=mode):
                    argv = ["upgrade", "--target", str(target)] + (["--dry-run"] if mode == "dry-run" else [])
                    code, stdout, stderr = run_cli_result(*argv)
                    self.assertEqual(2, code, stderr)
                    self.assertIn("UPGRADE_PENDING_ORDER", stderr)
                    self.assertIn("orch-upgrade", stderr)
                    self.assertIn(order["action_id"], stderr)
                    self.assertIn(order["phase"], stderr)
                    self.assertNotIn("Upgraded research workspace", stdout)
                    self.assertNotIn("would update", stdout)
                    self.assertEqual("# stale local copy\n", drifted.read_text(), "a refused upgrade must replace nothing")
                    self.assertEqual(log_before, (target / "log.md").read_text())
                    self.assertEqual(session_before, session_path.read_bytes())
                    self.assertFalse((target / ".locks" / "upgrade.lock").exists(), "no lock residue on refusal")
            self.assertEqual([], list((target / "runs" / "orchestrations" / "orch-upgrade" / ".locks").glob("*.holder.json")))

            # Drained: the same workspace with no pending order upgrades normally.
            session = json.loads(session_path.read_text(encoding="utf-8"))
            session["pending_action_id"] = None
            session_path.write_text(json.dumps(session, indent=2), encoding="utf-8")
            code, stdout, stderr = run_cli_result("upgrade", "--target", str(target))
            self.assertEqual(0, code, stderr)
            self.assertIn("Upgraded research workspace", stdout)
            self.assertEqual(TEMPLATE_QUERY_INDEX.read_bytes(), drifted.read_bytes())

    def test_unreadable_session_blocks_the_upgrade_conservatively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            session_dir = target / "runs" / "orchestrations" / "orch-garbled"
            session_dir.mkdir(parents=True)
            (session_dir / "session.json").write_text("{not json", encoding="utf-8")
            code, _stdout, stderr = run_cli_result("upgrade", "--target", str(target), "--dry-run")
            self.assertEqual(2, code, stderr)
            self.assertIn("UPGRADE_PENDING_ORDER", stderr)
            self.assertIn("orch-garbled", stderr)
            self.assertIn("unreadable", stderr)

    def test_upgrade_refuses_while_a_driver_holds_a_session_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = init_workspace(root / "workspace")
            code, _, stderr = controller(target, "start", "--orchestration-id", "orch-held", "--agent-id", "agent-a")
            self.assertEqual(0, code, stderr)
            lock_path = CONTROLLER.session_lock_path(target, "orch-held")
            with self.holding_lock(target, lock_path, root / "scratch-held"):
                drifted = target / "scripts" / "query_index.py"
                drifted.write_text("# stale local copy\n")

                def snapshot():
                    contents = {}
                    for path in target.rglob("*"):
                        if path.is_file():
                            if path == lock_path:
                                # Windows byte-range locks also prevent reads. Inspect
                                # the locked file's identity and metadata without opening it.
                                info = path.stat()
                                value = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
                            else:
                                value = path.read_bytes()
                            contents[path.relative_to(target)] = value
                    return contents

                before = snapshot()
                code, _stdout, stderr = run_cli_result("upgrade", "--target", str(target), "--dry-run")
                self.assertEqual(2, code, stderr)
                self.assertIn("UPGRADE_PENDING_ORDER", stderr)
                self.assertIn("active driver", stderr)
                self.assertEqual(before, snapshot())

                code, _stdout, stderr = run_cli_result("upgrade", "--target", str(target))

                self.assertEqual(2, code, stderr)
                self.assertIn("UPGRADE_PENDING_ORDER", stderr)
                self.assertIn("orch-held", stderr)
                self.assertIn("active driver", stderr)
                self.assertEqual(before, snapshot())
                self.assertFalse((target / ".locks" / "upgrade.lock").exists())

            code, stdout, stderr = run_cli_result("upgrade", "--target", str(target))
            self.assertEqual(0, code, stderr)
            self.assertIn("Upgraded research workspace", stdout)

    def test_upgrade_and_controller_agree_on_the_lock_and_session_layout(self):
        # Two scripts, one protocol: the paths are hand-mirrored, so compare them.
        self.assertEqual(CONTROLLER.UPGRADE_LOCK_RELATIVE, INIT.UPGRADE_LOCK_RELATIVE.as_posix())
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.assertEqual(CONTROLLER.upgrade_lock_path(root), INIT.upgrade_lock_path(root))
            self.assertEqual(
                CONTROLLER.session_lock_path(root, "orch"),
                CONTROLLER.session_dir(root, "orch") / Path(*INIT.ORCHESTRATION_SESSION_LOCK_RELATIVE.parts),
            )
            self.assertEqual(CONTROLLER.orchestration_root(root), root / Path(*INIT.ORCHESTRATION_SESSIONS_RELATIVE.parts))
        self.assertEqual(CONTROLLER.SESSION_FILENAME, INIT.ORCHESTRATION_SESSION_FILENAME)
        self.assertEqual(CONTROLLER.WORK_ORDERS_DIR, INIT.ORCHESTRATION_WORK_ORDERS_DIR)

    def test_driver_refuses_while_a_workspace_upgrade_holds_its_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = init_workspace(root / "workspace")
            self.assertFalse((target / ".locks" / "upgrade.lock").exists())
            code, _, stderr = controller(target, "start", "--orchestration-id", "orch-probe", "--agent-id", "agent-a")
            self.assertEqual(0, code, stderr)
            self.assertFalse((target / ".locks" / "upgrade.lock").exists(), "the probe must not create the lock file")

            with self.holding_lock(target, INIT.upgrade_lock_path(target), root / "scratch-upgrade"):
                code, payload, _stderr = controller(target, "start", "--orchestration-id", "orch-late", "--agent-id", "agent-a")
                self.assertNotEqual(0, code)
                self.assertEqual("ORCHESTRATION_UPGRADE_IN_PROGRESS", payload.get("error_code"), payload)
                self.assertIs(True, payload.get("recoverable"), payload)
                self.assertFalse((target / "runs" / "orchestrations" / "orch-late" / "session.json").exists())
                code, payload, _stderr = controller(target, "next", "--orchestration-id", "orch-probe", "--agent-id", "agent-a")
                self.assertEqual("ORCHESTRATION_UPGRADE_IN_PROGRESS", payload.get("error_code"), payload)

            code, payload, stderr = controller(target, "start", "--orchestration-id", "orch-late", "--agent-id", "agent-a")
            self.assertEqual(0, code, stderr)
            self.assertEqual("orch-late", payload.get("orchestration_id"))


if __name__ == "__main__":
    unittest.main()
