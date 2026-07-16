import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCORER_PATH = REPO_ROOT / "tools" / "score_eval_workspace.py"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "agent-quality-eval"
EVAL_DOC = REPO_ROOT / "workspace-template" / "docs" / "agent-quality-evaluation.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


def load_scorer():
    if not SCORER_PATH.is_file():
        raise AssertionError(f"missing scorer tool: {SCORER_PATH.relative_to(REPO_ROOT)}")
    spec = importlib.util.spec_from_file_location("score_eval_workspace", SCORER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load scorer from {SCORER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_yaml(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


class ScoreEvalWorkspaceTests(unittest.TestCase):
    def run_scorer(self, export_path: Path, expected_path: Path) -> dict:
        scorer = load_scorer()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = scorer.main(
                [
                    "--export",
                    str(export_path),
                    "--expected",
                    str(expected_path),
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, code)
        return json.loads(stdout.getvalue())

    def test_scores_answered_blocked_and_distractor_citations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            export_path = root / "export.json"
            expected_path = root / "expected.yml"
            expected_key = {
                "schema_version": "1.0",
                "questions": [
                    {
                        "slug": "cooling-corridors",
                        "expected_status": "answered",
                        "answer_page": "wiki/synthesis/cooling-corridors.md",
                        "required_answer_phrases": [
                            "shaded walking corridors",
                            "surface temperatures",
                        ],
                        "expected_source_ids": ["raw:cooling-corridors"],
                    },
                    {
                        "slug": "cooling-centers",
                        "expected_status": "answered",
                        "answer_page": "wiki/synthesis/cooling-centers.md",
                        "required_answer_phrases": [
                            "overnight cooling centers",
                            "24-hour staffing",
                        ],
                        "expected_source_ids": ["raw:cooling-centers"],
                    },
                    {
                        "slug": "maintenance-cost-gap",
                        "expected_status": "blocked",
                        "required_blocked_phrases": [
                            "maintenance cost memo",
                            "not delivered",
                        ],
                    },
                ],
            }
            export = {
                "schema_version": "1.0",
                "questions": [
                    {
                        "slug": "cooling-corridors",
                        "status": "answered",
                        "answer_page": "wiki/synthesis/cooling-corridors.md",
                        "answer_summary": (
                            "Shaded walking corridors reduced surface temperatures in the "
                            "synthetic pilot evidence."
                        ),
                        "source_ids": ["raw:cooling-corridors", "raw:parking-distractor"],
                    },
                    {
                        "slug": "cooling-centers",
                        "status": "answered",
                        "answer_page": "wiki/synthesis/cooling-centers.md",
                        "answer_summary": "The record supports overnight cooling centers.",
                        "source_ids": ["raw:cooling-centers"],
                    },
                    {
                        "slug": "maintenance-cost-gap",
                        "status": "blocked",
                        "answer_page": None,
                        "answer_summary": None,
                        "blocked_reason": "The maintenance cost memo was not delivered.",
                        "source_ids": [],
                    },
                ],
            }
            write_yaml(expected_path, expected_key)
            write_json(export_path, export)

            report = self.run_scorer(export_path, expected_path)

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual(3, report["counts"]["expected_questions"])
        self.assertEqual(3, report["counts"]["scored_questions"])
        self.assertEqual(0, report["counts"]["missing_questions"])
        self.assertEqual(0, report["counts"]["unexpected_questions"])
        self.assertEqual(255.0, report["score"]["points"])
        self.assertEqual(300.0, report["score"]["max_points"])
        self.assertEqual(85.0, report["score"]["percent"])

        by_slug = {question["slug"]: question for question in report["questions"]}
        self.assertEqual(90.0, by_slug["cooling-corridors"]["percent"])
        self.assertEqual(65.0, by_slug["cooling-centers"]["percent"])
        self.assertEqual(100.0, by_slug["maintenance-cost-gap"]["percent"])
        self.assertIn("raw:parking-distractor", by_slug["cooling-corridors"]["findings"][0])

    def test_missing_and_unexpected_questions_are_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            export_path = root / "export.json"
            expected_path = root / "expected.yml"
            write_yaml(
                expected_path,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {
                            "slug": "expected-question",
                            "expected_status": "answered",
                            "required_answer_phrases": ["expected phrase"],
                            "expected_source_ids": ["raw:expected"],
                        }
                    ],
                },
            )
            write_json(
                export_path,
                {
                    "schema_version": "1.0",
                    "questions": [
                        {
                            "slug": "unexpected-question",
                            "status": "answered",
                            "answer_summary": "Unexpected phrase.",
                            "source_ids": ["raw:unexpected"],
                        }
                    ],
                },
            )

            report = self.run_scorer(export_path, expected_path)

        self.assertEqual(1, report["counts"]["missing_questions"])
        self.assertEqual(1, report["counts"]["unexpected_questions"])
        self.assertEqual(0.0, report["score"]["percent"])
        self.assertIn("missing from export", report["questions"][0]["findings"][0])
        self.assertIn("unexpected-question", report["warnings"][0])

    def test_fixture_and_manual_harness_documentation_are_present(self):
        self.assertTrue((FIXTURE_ROOT / "workspace-init.yml").is_file())
        self.assertTrue((FIXTURE_ROOT / "expected-answers.yml").is_file())
        self.assertTrue((FIXTURE_ROOT / "delivery" / "raw" / "papers" / "parking-shade-distractor.md").is_file())
        key = yaml.safe_load((FIXTURE_ROOT / "expected-answers.yml").read_text(encoding="utf-8"))
        self.assertEqual("1.0", key["schema_version"])
        self.assertEqual(
            ["cooling-corridors", "cooling-centers", "maintenance-cost-gap"],
            [question["slug"] for question in key["questions"]],
        )

        doc = EVAL_DOC.read_text(encoding="utf-8")
        self.assertIn("tools/score_eval_workspace.py --export", doc)
        self.assertIn("manual/periodic", doc)
        self.assertIn("distractor", doc)

        contributing = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("Agent-quality evaluation is a manual check", contributing)
        self.assertIn("tools/score_eval_workspace.py", contributing)
        self.assertIn("--export /path/to/export.json", contributing)


if __name__ == "__main__":
    unittest.main()
