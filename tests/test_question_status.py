import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "question_status.py"


def load_module():
    return load_script_module("research_question_status", SCRIPT_PATH)


QSTATUS = load_module()

# CR-4: a domain pack may namespace its own request kinds. question_status reports on
# question frontmatter and links to requests only by id, so the kind vocabulary is
# inert here — these tests hold that independence.
PACK_REQUEST_KIND = "pack:market-data/supplier_quote"


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

    def write_blocked_on_request_question(self, root: Path) -> None:
        (root / "wiki" / "questions" / "blocked-on-quote.md").write_text(
            "---\ntype: question\nstatus: blocked\npriority: high\n"
            "question: What does the supplier charge?\n"
            "blocked_reason: Awaiting a live supplier quote.\n"
            "blocking_request_ids:\n"
            "  - req-pack-quote\n"
            "  - req-structured\n"
            "source_ids: []\n---\n# Q\n"
        )

    def write_extended_kind_requests(self, root: Path) -> None:
        """Declare a pack kind and open requests using it, ``structured_data``, and a scope map.

        Written as JSONL rather than through ``source_requests.py add`` so this unit
        does not depend on that command's in-flight ``--kind`` validation.
        """
        config_path = root / "research.yml"
        config_path.write_text(
            config_path.read_text()
            + "domain_pack:\n"
            "  name: market-data\n"
            "  request_kinds:\n"
            f"    - id: {PACK_REQUEST_KIND}\n"
            "      label: Supplier quote\n"
            "      description: Live SKU price from a named supplier.\n"
        )
        requests = root / "sources" / "source-requests.jsonl"
        requests.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "schema_version": "1.0",
                "request_id": "req-pack-quote",
                "kind": PACK_REQUEST_KIND,
                "query_or_identifier": "acme-widget list price",
                "rationale": "Blocks the pricing question.",
                "priority": "high",
                "question_slugs": ["blocked-on-quote"],
                "status": "open",
                "source_id": None,
                "scope": {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            },
            {
                "schema_version": "1.0",
                "request_id": "req-structured",
                "kind": "structured_data",
                "query_or_identifier": "regional price index series",
                "rationale": "Background series.",
                "priority": "medium",
                "question_slugs": [],
                "status": "open",
                "source_id": None,
            },
        ]
        requests.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def status_questions(self, root: Path) -> tuple[int, dict]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = QSTATUS.main(["--project-root", str(root), "--format", "json"])
        return code, json.loads(stdout.getvalue())

    def test_blocked_question_carries_request_ids_for_pack_namespaced_kinds(self):
        """CR-4: the detail view links to requests by id, so any kind flows through."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.write_extended_kind_requests(root)
            self.write_blocked_on_request_question(root)

            code, payload = self.status_questions(root)

        self.assertEqual(0, code)
        record = next(item for item in payload["questions"] if item["slug"] == "blocked-on-quote")
        self.assertEqual("blocked", record["status"])
        self.assertEqual(["req-pack-quote", "req-structured"], record["blocking_request_ids"])
        self.assertEqual(2, payload["by_status"]["blocked"])

    def test_extended_request_kinds_do_not_change_the_question_report(self):
        """The request store — pack kinds, ``structured_data``, scope maps — is inert here.

        question_status reports on question frontmatter only, so adding those records
        must leave the report byte-identical apart from its timestamp.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            without_dir = Path(tmpdir) / "without"
            without_dir.mkdir()
            without_root = self.build_workspace(without_dir)
            self.write_blocked_on_request_question(without_root)
            without_code, without_payload = self.status_questions(without_root)

            with_dir = Path(tmpdir) / "with"
            with_dir.mkdir()
            with_root = self.build_workspace(with_dir)
            self.write_extended_kind_requests(with_root)
            self.write_blocked_on_request_question(with_root)
            with_code, with_payload = self.status_questions(with_root)

        self.assertEqual(0, without_code)
        self.assertEqual(0, with_code)
        without_payload.pop("generated_at")
        with_payload.pop("generated_at")
        self.assertEqual(without_payload, with_payload)
        # Guard against the comparison passing because both reports are empty.
        self.assertEqual(2, with_payload["by_status"]["blocked"])

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
