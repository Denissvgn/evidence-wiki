import unittest
from pathlib import Path

from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
REVIEW_CONFIG_PATH = SCRIPTS / "_review_config.py"
TEMPLATE_RESEARCH_YML = REPO_ROOT / "workspace-template" / "research.yml"
RESEARCH_YML_DOC = REPO_ROOT / "workspace-template" / "docs" / "research-yml.md"


REVIEW = load_script_module("research_review_config", REVIEW_CONFIG_PATH)


class ReviewConfigDefaultTests(unittest.TestCase):
    def test_absent_section_uses_documented_defaults(self):
        self.assertEqual(
            REVIEW.review_config({"project": {"name": "fixture"}}),
            {"escalation_scope": "workspace", "max_pending_review_hours": 168},
        )

    def test_empty_section_uses_documented_defaults(self):
        self.assertEqual(
            REVIEW.review_config({"review": {}}),
            {"escalation_scope": "workspace", "max_pending_review_hours": 168},
        )

    def test_null_section_uses_documented_defaults(self):
        self.assertEqual(
            REVIEW.review_config({"review": None}),
            {"escalation_scope": "workspace", "max_pending_review_hours": 168},
        )

    def test_non_mapping_config_uses_documented_defaults(self):
        self.assertEqual(
            REVIEW.review_config([]),
            {"escalation_scope": "workspace", "max_pending_review_hours": 168},
        )


class ReviewConfigEscalationScopeTests(unittest.TestCase):
    def test_workspace_scope_is_accepted(self):
        config = {"review": {"escalation_scope": "workspace"}}
        self.assertEqual(REVIEW.review_config(config)["escalation_scope"], "workspace")

    def test_question_scope_is_accepted(self):
        config = {"review": {"escalation_scope": "question"}}
        self.assertEqual(REVIEW.review_config(config)["escalation_scope"], "question")

    def test_surrounding_whitespace_is_ignored(self):
        config = {"review": {"escalation_scope": "  question  "}}
        self.assertEqual(REVIEW.review_config(config)["escalation_scope"], "question")

    def test_empty_scope_value_keeps_the_default(self):
        config = {"review": {"escalation_scope": None}}
        self.assertEqual(REVIEW.review_config(config)["escalation_scope"], "workspace")

    def test_unknown_scope_is_rejected(self):
        config = {"review": {"escalation_scope": "Question"}}
        with self.assertRaises(REVIEW.ReviewConfigError) as caught:
            REVIEW.review_config(config)
        self.assertEqual(caught.exception.error_code, "CONFIG_INVALID")
        self.assertIn("escalation_scope", caught.exception.message)
        self.assertIn("'Question'", caught.exception.message)
        self.assertTrue(caught.exception.remediation)

    def test_non_string_scope_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError) as caught:
            REVIEW.review_config({"review": {"escalation_scope": 1}})
        self.assertEqual(caught.exception.error_code, "CONFIG_INVALID")


class ReviewConfigMaxPendingHoursTests(unittest.TestCase):
    def test_positive_integer_is_accepted(self):
        config = {"review": {"max_pending_review_hours": 24}}
        self.assertEqual(REVIEW.review_config(config)["max_pending_review_hours"], 24)

    def test_null_disables_the_age_check(self):
        config = {"review": {"max_pending_review_hours": None}}
        self.assertIsNone(REVIEW.review_config(config)["max_pending_review_hours"])

    def test_zero_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError) as caught:
            REVIEW.review_config({"review": {"max_pending_review_hours": 0}})
        self.assertEqual(caught.exception.error_code, "CONFIG_INVALID")
        self.assertIn("max_pending_review_hours", caught.exception.message)

    def test_negative_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError):
            REVIEW.review_config({"review": {"max_pending_review_hours": -1}})

    def test_non_integer_is_rejected(self):
        for value in ("168", 1.5, [168]):
            with self.subTest(value=value):
                with self.assertRaises(REVIEW.ReviewConfigError):
                    REVIEW.review_config({"review": {"max_pending_review_hours": value}})

    def test_boolean_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError):
            REVIEW.review_config({"review": {"max_pending_review_hours": True}})


class ReviewSectionShapeTests(unittest.TestCase):
    def test_non_mapping_section_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError) as caught:
            REVIEW.review_config({"review": "question"})
        self.assertEqual(caught.exception.error_code, "CONFIG_INVALID")
        self.assertIn("mapping", caught.exception.message)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(REVIEW.ReviewConfigError) as caught:
            REVIEW.review_config({"review": {"escalation_scopes": "question"}})
        self.assertEqual(caught.exception.error_code, "CONFIG_INVALID")
        self.assertIn("escalation_scopes", caught.exception.message)

    def test_experimental_prefixed_key_is_allowed(self):
        config = {"review": {"escalation_scope": "question", "x-approval-queue": "queue-42"}}
        self.assertEqual(REVIEW.review_config(config)["escalation_scope"], "question")


class ReviewConfigDocumentationTests(unittest.TestCase):
    def test_template_documents_the_optional_section(self):
        template = TEMPLATE_RESEARCH_YML.read_text(encoding="utf-8")
        for expected in (
            "# review:",
            "#   escalation_scope: workspace",
            "#   max_pending_review_hours: 168",
        ):
            self.assertIn(expected, template)

    def test_template_leaves_the_section_commented_out(self):
        import yaml

        document = yaml.safe_load(TEMPLATE_RESEARCH_YML.read_text(encoding="utf-8"))
        self.assertNotIn("review", document)
        self.assertEqual(
            REVIEW.review_config(document),
            {"escalation_scope": "workspace", "max_pending_review_hours": 168},
        )

    def test_research_yml_doc_describes_both_keys(self):
        doc = RESEARCH_YML_DOC.read_text(encoding="utf-8")
        self.assertIn("### `review`", doc)
        for expected in (
            "`escalation_scope`",
            "`max_pending_review_hours`",
            "`questions_awaiting_review`",
        ):
            self.assertIn(expected, doc)


if __name__ == "__main__":
    unittest.main()
