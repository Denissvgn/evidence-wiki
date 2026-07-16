import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
MINIMAL_FIXTURE = FIXTURES / "minimal-project"
ARXIV_FIXTURE = FIXTURES / "arxiv-source-project"
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("path_safety_source_inventory", "source_inventory.py")
NORMALIZE = load_script_module("path_safety_normalize_sources", "normalize_sources.py")
LINT = load_script_module("path_safety_lint", "lint.py")
SMOKE = load_script_module("path_safety_smoke", "smoke_validate_workspace.py")
QUERY = load_script_module("path_safety_query", "query_index.py")
QSTATUS = load_script_module("path_safety_question_status", "question_status.py")
MCP = load_script_module("path_safety_mcp", "serve_mcp.py")


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["script", *args]
    try:
        yield
    finally:
        sys.argv = old


class WorkspacePathSafetyTests(unittest.TestCase):
    def copy_fixture(self, fixture: Path, root: Path) -> Path:
        workspace = root / "workspace"
        shutil.copytree(fixture, workspace)
        return workspace

    def load_config(self, workspace: Path) -> dict:
        return yaml.safe_load((workspace / "research.yml").read_text())

    def write_config(self, workspace: Path, config: dict) -> None:
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    def run_inventory(self, workspace: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with patched_argv("--project-root", str(workspace)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = INVENTORY.main()
        return int(code or 0), stderr.getvalue()

    def run_normalize(self, workspace: Path, *args: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with patched_argv("--project-root", str(workspace), *args):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = NORMALIZE.main()
        return int(code or 0), stderr.getvalue()

    def run_lint(self, workspace: Path) -> dict:
        config = LINT.load_config(workspace)
        return LINT.run_checks(workspace, config)

    def call_mcp_tool(self, workspace: Path, name: str, arguments: dict) -> dict:
        server = MCP.ResearchWikiMcpServer(workspace)
        response = server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self.assertIsNotNone(response)
        self.assertNotIn("error", response)
        return response["result"]

    def issue_categories(self, results: dict) -> set[str]:
        return {issue["category"] for issue in results["issues"]}

    def test_inventory_rejects_manifest_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.copy_fixture(MINIMAL_FIXTURE, root)
            config = self.load_config(workspace)
            config["sources"]["manifest_path"] = "../escaped/manifest.jsonl"
            self.write_config(workspace, config)

            code, stderr = self.run_inventory(workspace)

            # The refusal now surfaces through the shared error envelope: a
            # non-zero exit and a stderr message, not a propagated SystemExit.
            self.assertEqual(2, code)
            self.assertFalse((root / "escaped" / "manifest.jsonl").exists())

        self.assertIn("sources.manifest_path", stderr)

    def test_inventory_rejects_escape_syntax_matrix_without_external_writes(self):
        unsafe_values = (
            "../escaped/manifest.jsonl",
            "sources\\..\\..\\escaped\\manifest.jsonl",
            "/absolute/manifest.jsonl",
            "C:\\research\\manifest.jsonl",
            "D:/research/manifest.jsonl",
            "\\\\server\\share\\manifest.jsonl",
            "//server/share/manifest.jsonl",
            "file:///tmp/manifest.jsonl",
            "https://example.org/manifest.jsonl",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            external_marker = root / "escaped" / "manifest.jsonl"
            for index, unsafe in enumerate(unsafe_values):
                with self.subTest(manifest_path=unsafe):
                    workspace = self.copy_fixture(MINIMAL_FIXTURE, root / str(index))
                    config = self.load_config(workspace)
                    config["sources"]["manifest_path"] = unsafe
                    self.write_config(workspace, config)

                    code, stderr = self.run_inventory(workspace)

                    self.assertEqual(2, code)
                    self.assertIn("sources.manifest_path", stderr)
                    self.assertFalse(external_marker.exists())

    def test_option_like_manifest_basename_stays_inside_generated_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.copy_fixture(MINIMAL_FIXTURE, root)
            marker = root / "must-not-exist"
            config = self.load_config(workspace)
            config["sources"]["manifest_path"] = "sources/--manifest;touch-must-not-exist.jsonl"
            self.write_config(workspace, config)

            code, stderr = self.run_inventory(workspace)

            self.assertEqual(0, code, stderr)
            self.assertTrue((workspace / "sources" / "--manifest;touch-must-not-exist.jsonl").is_file())
            self.assertFalse(marker.exists())

    def test_normalize_rejects_unsafe_normalized_dir_in_dry_run_and_write_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.copy_fixture(ARXIV_FIXTURE, root)
            self.run_inventory(workspace)
            config = self.load_config(workspace)
            config["sources"]["normalized_dir"] = "../escaped"
            self.write_config(workspace, config)

            for mode in ("dry-run", "write"):
                with self.subTest(mode=mode):
                    args = ("--all", "--dry-run") if mode == "dry-run" else ("--all",)
                    code, stderr = self.run_normalize(workspace, *args)
                    # Refusal flows through the shared error envelope (exit 2 +
                    # stderr message) rather than a propagated SystemExit.
                    self.assertEqual(2, code)
                    self.assertIn("sources.normalized_dir", stderr)
                    self.assertFalse((root / "escaped").exists())

    def test_lint_reports_high_config_issue_for_escaped_wiki_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.copy_fixture(MINIMAL_FIXTURE, root)
            outside = root / "outside-wiki"
            outside.mkdir()
            (outside / "external.md").write_text("---\ntype: concept\n---\n# External\n")
            config = self.load_config(workspace)
            config["wiki"]["root"] = "../outside-wiki"
            self.write_config(workspace, config)

            results = self.run_lint(workspace)

        self.assertIn("config_path", self.issue_categories(results))
        self.assertTrue(any(issue["severity"] == "HIGH" for issue in results["issues"]))

    def test_smoke_reports_high_config_issue_for_escaped_source_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_fixture(MINIMAL_FIXTURE, Path(tmpdir))
            config = self.load_config(workspace)
            config["sources"]["normalized_dir"] = "../escaped"
            self.write_config(workspace, config)

            results = SMOKE.run_checks(workspace)

        self.assertIn("config_path", self.issue_categories(results))
        self.assertTrue(any(issue["severity"] == "HIGH" for issue in results["issues"]))

    def test_query_rejects_unsafe_normalized_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_fixture(MINIMAL_FIXTURE, Path(tmpdir))
            config = self.load_config(workspace)
            config["sources"]["normalized_dir"] = "../escaped"
            self.write_config(workspace, config)

            with self.assertRaises(SystemExit) as context:
                QUERY.main(["--project-root", str(workspace), "--scope", "normalized", "fixture"])

        self.assertIn("sources.normalized_dir", str(context.exception))

    def test_mcp_query_rejects_unsafe_index_path_without_sqlite_side_effects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.copy_fixture(MINIMAL_FIXTURE, root)
            traversal_target = root.parent / f"{root.name}-traversal-index.sqlite3"
            absolute_target = root / "absolute-index.sqlite3"

            for unsafe, escaped_base in (
                (f"../../{traversal_target.name}", traversal_target),
                (str(absolute_target), absolute_target),
            ):
                with self.subTest(index_path=unsafe):
                    for suffix in ("", "-wal", "-journal"):
                        target = Path(f"{escaped_base}{suffix}")
                        if target.exists():
                            target.unlink()

                    result = self.call_mcp_tool(
                        workspace,
                        "query_index",
                        {"query": "fixture", "index_path": unsafe},
                    )

                    self.assertTrue(result["isError"])
                    payload = result["structuredContent"]
                    self.assertEqual("CONFIG_INVALID", payload["error_code"])
                    self.assertEqual("Workspace configuration is invalid.", payload["message"])
                    for suffix in ("", "-wal", "-journal"):
                        self.assertFalse(Path(f"{escaped_base}{suffix}").exists())

    def test_question_status_rejects_unsafe_wiki_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "research.yml").write_text("wiki:\n  root: ..\\escaped\n")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = QSTATUS.main(["--project-root", str(workspace), "--format", "json"])

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        payload = json.loads(stderr.getvalue())
        self.assertEqual("CONFIG_INVALID", payload["error_code"])
        self.assertIn("wiki.root", payload["message"])

    def test_json_smoke_output_includes_config_path_issue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_fixture(MINIMAL_FIXTURE, Path(tmpdir))
            config = self.load_config(workspace)
            config["sources"]["manifest_path"] = "https://example.org/manifest.jsonl"
            self.write_config(workspace, config)
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                exit_code = SMOKE.main(["--project-root", str(workspace), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(1, exit_code)
        self.assertIn("config_path", {issue["category"] for issue in payload["issues"]})


if __name__ == "__main__":
    unittest.main()
