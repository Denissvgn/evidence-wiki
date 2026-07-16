import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "snapshot_user_edits.py"


def load_script_module():
    spec = importlib.util.spec_from_file_location("research_snapshot_user_edits", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SNAPSHOT = load_script_module()


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["snapshot_user_edits.py", *args]
    try:
        yield
    finally:
        sys.argv = old


class SnapshotUserEditsTests(unittest.TestCase):
    def git(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def build_workspace(self, root: Path, *, wiki_root: str = "wiki", policy: str = "explicit") -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "research.yml").write_text(
            "wiki:\n"
            f"  root: {wiki_root}\n"
            "integrations:\n"
            "  git:\n"
            f"    snapshot_user_edits: {policy}\n"
        )
        for path in (
            wiki_root,
            "docs",
            "skills",
            "raw/papers",
            "sources/normalized",
            "sources/cards",
        ):
            (workspace / path).mkdir(parents=True, exist_ok=True)
        (workspace / "index.md").write_text("# Index\n")
        (workspace / "log.md").write_text("# Log\n")
        (workspace / wiki_root / "seed.md").write_text("# Seed\n")
        (workspace / "docs" / "seed.md").write_text("# Seed\n")

        self.git(workspace, "init")
        self.git(workspace, "config", "user.email", "tests@example.invalid")
        self.git(workspace, "config", "user.name", "Snapshot Tests")
        self.git(workspace, "add", "--all")
        self.git(workspace, "commit", "-m", "initial workspace")
        return workspace

    def run_script(self, workspace: Path, *args: str) -> str:
        stdout = io.StringIO()
        with patched_argv("--project-root", str(workspace), *args):
            with contextlib.redirect_stdout(stdout):
                SNAPSHOT.main()
        return stdout.getvalue()

    def test_dry_run_reports_scoped_paths_without_staging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "wiki" / "new-note.md").write_text("# New note\n")
            (workspace / "docs" / "guide.md").write_text("# Guide\n")

            output = self.run_script(workspace)
            staged = self.git(workspace, "diff", "--cached", "--name-only").stdout

        self.assertIn("- wiki", output)
        self.assertIn("- index.md", output)
        self.assertIn("- log.md", output)
        self.assertIn("- docs", output)
        self.assertIn("- skills", output)
        self.assertIn("Dry run only; no files were staged or committed.", output)
        self.assertEqual("", staged)

    def test_requires_explicit_snapshot_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), policy="disabled")

            with self.assertRaises(SystemExit) as context:
                self.run_script(workspace)

        self.assertIn("snapshot_user_edits", str(context.exception))

    def test_rejects_unsafe_extra_paths_before_git_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            outside = Path(tmpdir) / "outside.md"
            unsafe_paths = [
                "../outside",
                str(outside),
                "raw/papers",
                "sources/manifest.jsonl",
                "sources/normalized/foo.md",
                "sources/cards/foo.md",
            ]

            for unsafe_path in unsafe_paths:
                with self.subTest(unsafe_path=unsafe_path):
                    with self.assertRaises(SystemExit):
                        self.run_script(workspace, "--path", unsafe_path)

    def test_commit_refuses_when_staged_changes_already_exist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "docs" / "staged.md").write_text("# Staged\n")
            self.git(workspace, "add", "docs/staged.md")

            with self.assertRaises(SystemExit) as context:
                self.run_script(workspace, "--commit")

        self.assertIn("Commit or unstage existing index changes", str(context.exception))

    def test_commit_snapshots_only_scoped_human_editable_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            (workspace / "wiki" / "new-note.md").write_text("# New note\n")
            (workspace / "docs" / "guide.md").write_text("# Guide\n")
            (workspace / "raw" / "papers" / "raw.txt").write_text("raw evidence\n")
            (workspace / "sources" / "normalized" / "generated.md").write_text("generated\n")

            self.run_script(workspace, "--commit", "--message", "snapshot: test edits")
            committed = self.git(workspace, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
            status = self.git(workspace, "status", "--porcelain", "--untracked-files=all").stdout

        self.assertIn("wiki/new-note.md", committed)
        self.assertIn("docs/guide.md", committed)
        self.assertNotIn("raw/papers/raw.txt", committed)
        self.assertNotIn("sources/normalized/generated.md", committed)
        self.assertIn("?? raw/papers/raw.txt", status)
        self.assertIn("?? sources/normalized/generated.md", status)

    def test_custom_wiki_root_is_reported_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), wiki_root="knowledge")
            (workspace / "knowledge" / "note.md").write_text("# Note\n")

            output = self.run_script(workspace)

        self.assertIn("- knowledge", output)
        self.assertNotIn("- wiki\n", output)

    def test_absolute_configured_wiki_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), wiki_root="wiki")
            config = workspace / "research.yml"
            config.write_text(config.read_text().replace("root: wiki", f"root: {workspace / 'wiki'}"))

            with self.assertRaises(SystemExit) as context:
                self.run_script(workspace)

        self.assertIn("wiki.root", str(context.exception))


if __name__ == "__main__":
    unittest.main()
