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


VERIFY = load_script_module("research_verify_quotes", "verify_quotes.py")


class VerifyQuotesTests(unittest.TestCase):
    source_id = "web:vendor-official-product-spec"

    def make_workspace(self, root: Path, *, quote: str | None = "Vendor-controlled product specification.") -> Path:
        target = root / "grounding-workspace"
        (target / "wiki" / "questions").mkdir(parents=True)
        (target / "sources" / "normalized").mkdir(parents=True)
        (target / "research.yml").write_text(
            "project:\n  name: Grounding Fixture\n",
            encoding="utf-8",
        )
        grounding = []
        if quote is not None:
            grounding = [
                {
                    "claim": "The product spec is vendor-controlled.",
                    "source_id": self.source_id,
                    "quote": quote,
                    "location_hint": "Official product spec",
                }
            ]
        question = {
            "type": "question",
            "status": "answered",
            "question": "What is the vendor product spec?",
            "source_ids": [self.source_id],
            "answer_page": "../synthesis/vendor-product-answer.md",
            "coverage_required": True,
            "answered_by": "answer-agent",
            "grounding": grounding,
        }
        (target / "wiki" / "questions" / "vendor-product-spec.md").write_text(
            "---\n" + yaml.safe_dump(question, sort_keys=False) + "---\n\n# Vendor Product Spec\n",
            encoding="utf-8",
        )
        normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
        normalized.write_text(
            f"""---
type: normalized_source
source_id: {self.source_id}
title: Official product spec
---

# Official product spec

Vendor-controlled product specification.
""",
            encoding="utf-8",
        )
        return target

    def run_verify(self, target: Path, *extra: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.main(["--project-root", str(target), "--slug", "vendor-product-spec", *extra])
        payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}
        return int(code or 0), payload, stderr.getvalue()

    def question_frontmatter(self, target: Path) -> dict:
        text = (target / "wiki" / "questions" / "vendor-product-spec.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def test_quote_in_normalized_record_verifies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", payload["questions"][0]["grounding"][0]["result"])
        self.assertTrue(payload["questions"][0]["all_verified"])

    def test_quote_verification_normalizes_whitespace_and_case(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), quote="vendor-controlled   PRODUCT specification.")

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", payload["questions"][0]["grounding"][0]["result"])

    def test_missing_quote_reports_quote_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), quote="A different claim anchor.")

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(1, code, stderr)
        self.assertEqual("quote_not_found", payload["questions"][0]["grounding"][0]["result"])
        self.assertFalse(payload["questions"][0]["all_verified"])

    def test_quote_verification_normalizes_curly_quotes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), quote="It's the vendor's official spec.")
            normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
            normalized.write_text(
                "---\ntype: normalized_source\nsource_id: web:vendor-official-product-spec\n"
                "title: Official product spec\n---\n\n"
                "# Official product spec\n\n"
                "It’s the vendor’s official spec.\n",
                encoding="utf-8",
            )

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", payload["questions"][0]["grounding"][0]["result"])

    def test_quote_verification_collapses_hyphenation_at_line_break(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), quote="a vendor-controlled specification")
            normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
            normalized.write_text(
                "---\ntype: normalized_source\nsource_id: web:vendor-official-product-spec\n"
                "title: Official product spec\n---\n\n"
                "# Official product spec\n\n"
                "This is a vendor-\ncontrolled specification.\n",
                encoding="utf-8",
            )

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", payload["questions"][0]["grounding"][0]["result"])

    def test_quote_anchor_repetition_ocr_hyphenation_and_negative_matrix(self):
        cases = [
            {
                "name": "repeated_section_anchored",
                "quote": "Retained evidence sentence.",
                "location_hint": "Findings",
                "body": (
                    "# Background\n\nRetained evidence sentence.\n\n"
                    "# Findings\n\nRetained evidence sentence.\n"
                ),
                "result": "verified",
                "anchor_type": "section",
                "global_count": 2,
            },
            {
                "name": "page_anchor",
                "quote": "Page-specific retained evidence.",
                "location_hint": "page 2",
                "body": (
                    "<!-- page: 1 -->\nUnrelated text.\n"
                    "<!-- page: 2 -->\nPage-specific retained evidence.\n"
                ),
                "result": "verified",
                "anchor_type": "page",
            },
            {
                "name": "repeated_unanchored",
                "quote": "Repeated evidence.",
                "location_hint": None,
                "body": "# Evidence\n\nRepeated evidence.\n\nRepeated evidence.\n",
                "result": "quote_ambiguous",
            },
            {
                "name": "ocr_ligature",
                "quote": "official specification",
                "location_hint": "Evidence",
                "body": "# Evidence\n\nThe ofﬁcial speciﬁcation is retained.\n",
                "result": "verified",
                "match_type": "normalized",
            },
            {
                "name": "word_hyphenation",
                "quote": "official specification",
                "location_hint": "Evidence",
                "body": "# Evidence\n\nThe official specifi-\ncation is retained.\n",
                "result": "verified",
                "match_type": "normalized_dehyphenated",
            },
            {
                "name": "altered_meaning",
                "quote": "The system does support unsafe execution.",
                "location_hint": "Evidence",
                "body": "# Evidence\n\nThe system does not support unsafe execution.\n",
                "result": "quote_not_found",
            },
            {
                "name": "wrong_section",
                "quote": "Anchored evidence.",
                "location_hint": "Findings",
                "body": "# Background\n\nAnchored evidence.\n\n# Findings\n\nDifferent evidence.\n",
                "result": "quote_not_at_anchor",
            },
            {
                "name": "missing_anchor",
                "quote": "Anchored evidence.",
                "location_hint": "Missing Section",
                "body": "# Evidence\n\nAnchored evidence.\n",
                "result": "anchor_not_found",
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(Path(tmpdir), quote=case["quote"])
                normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
                normalized.write_text(
                    "---\ntype: normalized_source\nsource_id: web:vendor-official-product-spec\n"
                    "title: Official product spec\n---\n\n"
                    + case["body"],
                    encoding="utf-8",
                )
                question = target / "wiki" / "questions" / "vendor-product-spec.md"
                frontmatter = self.question_frontmatter(target)
                grounding = frontmatter["grounding"][0]
                if case["location_hint"] is None:
                    grounding.pop("location_hint", None)
                else:
                    grounding["location_hint"] = case["location_hint"]
                question.write_text(
                    "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Vendor Product Spec\n",
                    encoding="utf-8",
                )

                code, payload, stderr = self.run_verify(target)

            result = payload["questions"][0]["grounding"][0]
            expected_code = 0 if case["result"] == "verified" else 1
            self.assertEqual(expected_code, code, stderr)
            self.assertEqual(case["result"], result["result"])
            self.assertEqual("retained_quote_evidence", result["policy"])
            self.assertTrue(result["artifacts"])
            self.assertTrue(result["remediation"])
            if "anchor_type" in case:
                self.assertEqual(case["anchor_type"], result["anchor"]["type"])
                self.assertEqual("matched", result["anchor"]["status"])
            if "global_count" in case:
                self.assertEqual(case["global_count"], result["global_occurrence_count"])
            if "match_type" in case:
                self.assertEqual(case["match_type"], result["match_type"])

    def test_non_normalized_source_reports_source_not_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            (target / "sources" / "normalized" / "web--vendor-official-product-spec.md").unlink()

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(1, code, stderr)
        self.assertEqual("source_not_normalized", payload["questions"][0]["grounding"][0]["result"])

    def test_malformed_grounding_uses_json_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "vendor-product-spec.md"
            frontmatter = self.question_frontmatter(target)
            frontmatter["grounding"][0].pop("quote")
            question.write_text(
                "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Vendor Product Spec\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = VERIFY.main(["--project-root", str(target), "--slug", "vendor-product-spec", "--format", "json"])

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("GROUNDING_INVALID", json.loads(stderr.getvalue())["error_code"])

    def test_path_like_slug_is_refused_before_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = VERIFY.main(
                    ["--project-root", str(target), "--slug", "../vendor-product-spec", "--format", "json"]
                )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("SLUG_INVALID", json.loads(stderr.getvalue())["error_code"])

    def test_write_mode_records_distinct_verifier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, payload, stderr = self.run_verify(target, "--write", "--verified-by", "verify-agent")

            frontmatter = self.question_frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["questions"][0]["all_verified"])
        self.assertEqual("verify-agent", frontmatter["verified_by"])
        self.assertIn("grounding_verified_at", frontmatter)


if __name__ == "__main__":
    unittest.main()
