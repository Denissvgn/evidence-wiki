import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def write_parked_question(
        self,
        root: Path,
        *,
        requested_at: str | None,
        accepted_policy: str | None = None,
    ) -> None:
        clock = f'human_review_requested_at: "{requested_at}"\n' if requested_at else ""
        reviews = ""
        if accepted_policy is not None:
            reviews = (
                "human_reviews:\n"
                f'  - policy: "{accepted_policy}"\n'
                "    verdict: accepted\n"
                "    reviewed_by: ops-principal\n"
                '    reviewed_at: "2026-08-07T10:00:00Z"\n'
            )
        (root / "wiki" / "questions" / "parked.md").write_text(
            "---\ntype: question\nstatus: human_review\npriority: high\n"
            "question: Which review clears this?\n"
            "answer_page: ../synthesis/answer.md\nsource_ids:\n  - paper:x\n"
            "human_review_required: true\nhuman_review_status: pending\n"
            f"{clock}"
            "human_review_policies:\n"
            "  - manual_review_required\n"
            '  - "pack:market-data/quote-48h"\n'
            f"{reviews}"
            "---\n# Q\n"
        )

    def test_review_queue_records_carry_age_and_pending_policies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.write_parked_question(
                root,
                requested_at="2026-08-07T09:00:00Z",
                accepted_policy="pack:market-data/quote-48h",
            )
            config = QSTATUS.load_config(root)
            records = QSTATUS.collect_questions(QSTATUS.questions_directory(root, config))
            record = next(item for item in records if item["slug"] == "parked")

        self.assertEqual("2026-08-07T09:00:00Z", record["human_review_requested_at"])
        self.assertEqual(["manual_review_required"], record["human_review_pending_policies"])

    def test_review_queue_reports_every_policy_pending_before_any_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.write_parked_question(root, requested_at="2026-08-07T09:00:00Z")
            config = QSTATUS.load_config(root)
            records = QSTATUS.collect_questions(QSTATUS.questions_directory(root, config))
            record = next(item for item in records if item["slug"] == "parked")

        self.assertEqual(
            ["manual_review_required", "pack:market-data/quote-48h"],
            record["human_review_pending_policies"],
        )

    def test_text_output_shows_review_age_and_pending_policy_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            aged = datetime.now(timezone.utc) - timedelta(hours=30)
            self.write_parked_question(
                root,
                requested_at=aged.strftime("%Y-%m-%dT%H:%M:%SZ"),
                accepted_policy="pack:market-data/quote-48h",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                QSTATUS.main(["--project-root", str(root)])
            text = stdout.getvalue()

        self.assertIn("Pending Human Review (1):", text)
        self.assertRegex(text, r"- parked \[waiting 30\.\d+h, 1 policy\(ies\) pending\]: Which review clears this\?")

    def test_text_output_reports_unknown_age_without_a_review_clock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.write_parked_question(root, requested_at=None)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                QSTATUS.main(["--project-root", str(root)])
            text = stdout.getvalue()

        self.assertIn("- parked [age unknown, 2 policy(ies) pending]:", text)

    def test_review_age_hours_rejects_an_unparseable_clock(self):
        self.assertIsNone(QSTATUS.review_age_hours("not-a-timestamp"))
        self.assertIsNone(QSTATUS.review_age_hours(None))
        self.assertAlmostEqual(
            24.0,
            QSTATUS.review_age_hours(
                "2026-08-06T09:00:00Z",
                now=datetime(2026, 8, 7, 9, 0, 0, tzinfo=timezone.utc),
            ),
        )

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
