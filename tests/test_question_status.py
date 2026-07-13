import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "question_status.py"


def load_module():
    spec = importlib.util.spec_from_file_location("research_question_status", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


QSTATUS = load_module()


class QuestionStatusTests(unittest.TestCase):
    def build_workspace(self, root: Path) -> Path:
        (root / "research.yml").write_text("wiki:\n  root: wiki\n")
        questions = root / "wiki" / "questions"
        questions.mkdir(parents=True)
        (questions / "open-high.md").write_text(
            "---\ntype: question\nstatus: open\npriority: high\n"
            "origin: parent_agent\nquestion: High priority open question?\n"
            "source_ids: []\n---\n# Q\n"
        )
        (questions / "open-low.md").write_text(
            "---\ntype: question\nstatus: open\npriority: low\n"
            "question: Low priority open question?\nsource_ids: []\n---\n# Q\n"
        )
        (questions / "blocked.md").write_text(
            "---\ntype: question\nstatus: blocked\n"
            "blocked_reason: Needs a 2024 source.\nsource_ids: []\n---\n# Q\n"
        )
        (questions / "answered.md").write_text(
            "---\ntype: question\nstatus: answered\n"
            "answer_page: ../synthesis/answer.md\nsource_ids:\n  - paper:x\n---\n# Q\n"
        )
        (questions / "not-a-question.md").write_text("---\ntype: concept\n---\n# C\n")
        return root

    def test_collect_and_report_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            config = QSTATUS.load_config(root)
            questions_dir = QSTATUS.questions_directory(root, config)
            records = QSTATUS.collect_questions(questions_dir)
            report = QSTATUS.build_report(records)

        self.assertEqual(4, report["total"])
        self.assertEqual({"open": 2, "blocked": 1, "answered": 1}, report["by_status"])
        self.assertEqual(2, report["actionable"])
        self.assertEqual(1, report["blocked"])
        self.assertEqual(1, report["answered"])

    def test_json_output_lists_questions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                QSTATUS.main(["--project-root", str(root), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual("wiki/questions", payload["questions_dir"])
        self.assertEqual(4, payload["total"])
        slugs = {record["slug"] for record in payload["questions"]}
        self.assertIn("open-high", slugs)
        self.assertNotIn("not-a-question", slugs)

    def test_status_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                QSTATUS.main(["--project-root", str(root), "--format", "json", "--status", "blocked"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(1, payload["total"])
        self.assertEqual("blocked", payload["questions"][0]["status"])

    def test_text_output_lists_actionable_backlog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                QSTATUS.main(["--project-root", str(root)])
            text = stdout.getvalue()

        self.assertIn("Actionable backlog (2):", text)
        self.assertIn("open-high", text)

    def test_missing_questions_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text("wiki:\n  root: wiki\n")
            config = QSTATUS.load_config(root)
            questions_dir = QSTATUS.questions_directory(root, config)
            records = QSTATUS.collect_questions(questions_dir)

        self.assertEqual([], records)


if __name__ == "__main__":
    unittest.main()
