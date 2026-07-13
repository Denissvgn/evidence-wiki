"""End-to-end pipeline test: inventory → normalize → lint → query.

Covers the four deterministic script stages that can run without an LLM.
Ingest (agent-driven wiki source notes) is intentionally excluded — it requires
a language model and is tested separately via skill smoke-tests.
"""

import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARXIV_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "arxiv-source-project"

INVENTORY_PATH = REPO_ROOT / "workspace-template" / "scripts" / "source_inventory.py"
NORMALIZE_PATH = REPO_ROOT / "workspace-template" / "scripts" / "normalize_sources.py"
LINT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "lint.py"
QUERY_PATH = REPO_ROOT / "workspace-template" / "scripts" / "query_index.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = _load_module("research_source_inventory_e2e", INVENTORY_PATH)
NORMALIZE = _load_module("research_normalize_sources_e2e", NORMALIZE_PATH)
LINT = _load_module("evidence_wiki_lint_e2e", LINT_PATH)
QUERY = _load_module("research_query_index_e2e", QUERY_PATH)


@contextmanager
def _patched_argv(*args: str):
    """Temporarily replace sys.argv for scripts whose parse_args() reads it directly."""
    old = sys.argv
    sys.argv = ["script"] + list(args)
    try:
        yield
    finally:
        sys.argv = old


class PipelineE2ETests(unittest.TestCase):
    """Verify that inventory, normalize, lint, and query form a working data pipeline."""

    def _run_inventory(self, workspace: Path) -> int:
        devnull = io.StringIO()
        with _patched_argv("--project-root", str(workspace)):
            with contextlib.redirect_stdout(devnull):
                return INVENTORY.main()

    def _run_normalize(self, workspace: Path) -> int:
        devnull = io.StringIO()
        with _patched_argv("--project-root", str(workspace), "--all"):
            with contextlib.redirect_stdout(devnull):
                return NORMALIZE.main()

    def test_inventory_normalize_lint_query_chain(self):
        """Full pipeline: inventory → normalize → lint (no HIGH) → query returns results.

        The arXiv fixture provides a synthetic LaTeX bundle and link inputs so that
        all four stages run without pdftotext or network access.

        Ingest gap: agent-driven wiki source notes are not produced here; the lint
        stage will report LOW source_missing_noted issues for the new normalized
        records, which is expected and accepted by this test.
        """
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            shutil.copytree(ARXIV_FIXTURE, workspace)

            # ── Stage 1: Inventory ────────────────────────────────────────────
            exit_code = self._run_inventory(workspace)
            self.assertEqual(0, exit_code, "inventory script returned non-zero")

            manifest_path = workspace / "sources" / "manifest.jsonl"
            self.assertTrue(manifest_path.is_file(), "manifest.jsonl was not created")

            records = [
                json.loads(line)
                for line in manifest_path.read_text().splitlines()
                if line.strip()
            ]
            self.assertGreater(len(records), 0, "manifest has no records after inventory")
            self.assertTrue(
                any(r.get("kind") == "paper" for r in records),
                "manifest contains no paper records",
            )

            # ── Stage 2: Normalize ────────────────────────────────────────────
            # LaTeX bundle → content_extracted; link records → link stubs.
            # PDF record is paired with the LaTeX bundle so LaTeX method is used;
            # pdftotext is not required.
            exit_code = self._run_normalize(workspace)
            self.assertEqual(0, exit_code, "normalize script returned non-zero")

            normalized_dir = workspace / "sources" / "normalized"
            normalized_files = list(normalized_dir.rglob("*.md"))
            self.assertGreater(
                len(normalized_files), 0, "no normalized records were created"
            )

            # ── Stage 3: Lint ─────────────────────────────────────────────────
            config = LINT.load_config(workspace)
            results = LINT.run_checks(workspace, config)
            high_issues = [i for i in results["issues"] if i["severity"] == "HIGH"]
            self.assertEqual(
                [],
                high_issues,
                f"Unexpected HIGH lint issues after normalize: {high_issues}",
            )

            # ── Stage 4: Query ────────────────────────────────────────────────
            # "synthetic" appears in the abstract of the arXiv fixture paper.
            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                exit_code = QUERY.main([
                    "--project-root", str(workspace),
                    "--format", "json",
                    "--scope", "normalized",
                    "synthetic",
                ])
            self.assertEqual(0, exit_code, "query script returned non-zero")

            payload = json.loads(stdout_buf.getvalue())
            self.assertGreater(
                payload["result_count"],
                0,
                "query for 'synthetic' returned no results in the normalized scope",
            )

    def test_query_results_reference_normalized_source(self):
        """Query results from stage 4 should resolve to paths under sources/normalized/."""
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            shutil.copytree(ARXIV_FIXTURE, workspace)

            self._run_inventory(workspace)
            self._run_normalize(workspace)

            stdout_buf = io.StringIO()
            with contextlib.redirect_stdout(stdout_buf):
                QUERY.main([
                    "--project-root", str(workspace),
                    "--format", "json",
                    "--scope", "normalized",
                    "fixture",
                ])

            payload = json.loads(stdout_buf.getvalue())
            self.assertGreater(payload["result_count"], 0)
            for result in payload["results"]:
                self.assertIn(
                    "sources/normalized",
                    result["path"],
                    f"Result path {result['path']!r} is not under sources/normalized/",
                )
