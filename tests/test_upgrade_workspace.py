import contextlib
import importlib.util
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli  # noqa: E402

INIT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "init_research_workspace.py"
TEMPLATE_QUERY_INDEX = REPO_ROOT / "workspace-template" / "scripts" / "query_index.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("research_init_for_upgrade", INIT_PATH)


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

    def test_upgrade_noop_when_current(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = init_workspace(Path(tmpdir) / "workspace")
            log_before = (target / "log.md").read_text()

            code, output = run_cli("upgrade", "--target", str(target))

            self.assertEqual(code, 0)
            self.assertIn("no changes", output)
            # A no-op upgrade should not append an upgrade log entry.
            self.assertEqual((target / "log.md").read_text(), log_before)

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
            self.assertIn('starter_version: "0.4.0"', (workspace / "workspace-system.yml").read_text())

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


if __name__ == "__main__":
    unittest.main()
