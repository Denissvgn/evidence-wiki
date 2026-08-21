import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


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

    def test_quote_entries_are_tagged_and_counted_as_quote_form(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        question = payload["questions"][0]
        self.assertEqual("quote", question["grounding"][0]["form"])
        self.assertEqual("retained_quote_evidence", question["grounding"][0]["policy"])
        self.assertEqual({"quote": 1, "anchor": 0}, question["by_form"])
        self.assertEqual({"quote": 1, "anchor": 0}, payload["counts"]["by_form"])

    def test_counts_keep_their_existing_keys_and_order(self):
        # `orchestration_controller` mirrors this dict in its empty-workspace fallback, so
        # the shape is a contract with a consumer that cannot see this report to copy it.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual(
            ["questions", "grounding_entries", "verified", "failed", "missing_grounding", "by_form"],
            list(payload["counts"]),
        )


class SeamTests(unittest.TestCase):
    """``run_verify`` is the same operation ``main`` runs, minus the printing (CR-6 T8).

    ``tests/test_seam_conformance.py`` holds the two renderings to each other
    permanently. What is pinned here is what a document comparison cannot see: that a
    failed verification is a returned report rather than an exception, that a refusal
    keeps this command's bare-message text rendering, and that ``--write`` stamps the
    question pages once on the way through the seam -- not twice, and not zero times.

    The fixture is the quote workspace next door, reused rather than rebuilt so the
    seam is exercised against exactly the evidence the CLI tests use.
    """

    source_id = VerifyQuotesTests.source_id
    make_workspace = VerifyQuotesTests.make_workspace
    question_frontmatter = VerifyQuotesTests.question_frontmatter

    def run_main(self, target: Path, *extra: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.main(["--project-root", str(target), "--slug", "vendor-product-spec", *extra])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def without_generated_at(document: dict) -> dict:
        return {key: value for key, value in document.items() if key != "generated_at"}

    def test_the_seam_returns_the_document_main_prints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_main(target, "--format", "json")
            returned = VERIFY.run_verify(target, ["vendor-product-spec"])

        self.assertEqual(0, code, stderr)
        self.assertIn("generated_at", returned)
        self.assertEqual(self.without_generated_at(json.loads(stdout)), self.without_generated_at(returned))

    def test_a_failed_verification_is_a_returned_report_not_a_refusal(self):
        # The whole value of the report is naming which claim failed and why. A seam that
        # raised here would hand a host an exception where the answer should have been.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir), quote="A sentence no retained record carries.")

            code, stdout, stderr = self.run_main(target, "--format", "json")
            returned = VERIFY.run_verify(target, ["vendor-product-spec"])

        self.assertEqual(VERIFY.EXIT_NOT_VERIFIED, code, stderr)
        self.assertEqual("not_verified", json.loads(stdout)["overall_result"])
        self.assertEqual("not_verified", returned["overall_result"])
        self.assertEqual("quote_not_found", returned["questions"][0]["grounding"][0]["result"])

    def test_a_refusal_carries_the_envelope_and_exit_code_the_cli_emits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            frontmatter = self.question_frontmatter(target)
            frontmatter["grounding"][0].pop("quote")
            (target / "wiki" / "questions" / "vendor-product-spec.md").write_text(
                "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Vendor Product Spec\n",
                encoding="utf-8",
            )

            code, stdout, stderr = self.run_main(target, "--format", "json")
            with self.assertRaises(VERIFY.VerifyQuotesError) as raised:
                VERIFY.run_verify(target, ["vendor-product-spec"])

        self.assertEqual("", stdout)
        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual(VERIFY.EXIT_INVALID, raised.exception.exit_code)
        self.assertEqual("GROUNDING_INVALID", raised.exception.error_code)
        self.assertEqual(json.loads(stderr), raised.exception.to_envelope())

    def test_an_unreadable_workspace_refuses_through_the_system_exit_funnel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "not-a-workspace"
            missing.mkdir()

            code, stdout, stderr = self.run_main(missing, "--format", "json")
            with self.assertRaises(VERIFY.ScriptRefusal) as raised:
                VERIFY.run_verify(missing, ["vendor-product-spec"])

        self.assertEqual("", stdout)
        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual(VERIFY.EXIT_INVALID, raised.exception.exit_code)
        self.assertEqual("CONFIG_MISSING", raised.exception.error_code)
        self.assertEqual(json.loads(stderr), raised.exception.to_envelope())

    def test_text_mode_still_prints_the_bare_refusal_message(self):
        # This command's coded refusals reached emit_error, which prints the message
        # alone. The shared refusal type would otherwise prefix `refused (CODE): `.
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_main(target, "--format", "text", "--write")

        self.assertEqual(VERIFY.EXIT_INVALID, code)
        self.assertEqual("", stdout)
        self.assertEqual("--verified-by is required when --write is set.\n", stderr)

    def test_write_stamps_through_the_seam_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            with mock.patch.object(
                VERIFY,
                "write_verification_metadata",
                wraps=VERIFY.write_verification_metadata,
            ) as stamp:
                code, _, stderr = self.run_main(target, "--format", "json", "--write", "--verified-by", "verify-agent")
                calls = stamp.call_count

            frontmatter = self.question_frontmatter(target)

        self.assertEqual(0, code, stderr)
        self.assertEqual(1, calls, "the --write mutation must happen once on the way through the seam")
        self.assertEqual("verify-agent", frontmatter["verified_by"])
        self.assertIn("grounding_verified_at", frontmatter)

    def test_the_seam_does_not_stamp_unless_write_is_requested(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            before = (target / "wiki" / "questions" / "vendor-product-spec.md").read_bytes()

            VERIFY.run_verify(target, ["vendor-product-spec"])

            after = (target / "wiki" / "questions" / "vendor-product-spec.md").read_bytes()

        self.assertEqual(before, after)

    def test_the_seam_de_duplicates_slugs_as_the_cli_does(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            returned = VERIFY.run_verify(target, ["vendor-product-spec", " vendor-product-spec "])

        self.assertEqual(1, returned["counts"]["questions"])

    def test_no_slugs_refuses_before_any_question_is_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            with self.assertRaises(VERIFY.VerifyQuotesError) as raised:
                VERIFY.run_verify(target, [])

        self.assertEqual("SLUG_INVALID", raised.exception.error_code)


_DEFAULT_SIDECAR = object()


class AnchorGroundingTests(unittest.TestCase):
    """Anchor-form grounding: the cited field holds exactly the value the claim states."""

    source_id = "data:keepa-b0abc123"
    safe_id = "data--keepa-b0abc123"
    slug = "supplier-price"
    record_body = "Supplier quote retained for the current period."

    sidecar_document = {
        "supplier_quote": {
            "price": "23.99 EUR",
            "amount": 23.99,
            "currency": "EUR",
            "in_stock": True,
            "discount": None,
            "breakdown": {"net": "19.99 EUR", "vat": "4.00 EUR"},
        },
        "history": [{"price": "21.50 EUR"}, {"price": "23.99 EUR"}],
        "odd/key": "slash token",
    }

    def anchor_entry(self, *, pointer="supplier_quote/price", expected="23.99 EUR", **extra) -> dict:
        entry = {
            "claim": "The current supplier price is 23.99 EUR.",
            "source_id": self.source_id,
            "anchor": {"pointer": pointer, "expected": expected},
        }
        entry.update(extra)
        return entry

    def quote_entry(self, *, quote=None, **extra) -> dict:
        entry = {
            "claim": "The supplier quote is retained evidence.",
            "source_id": self.source_id,
            "quote": self.record_body if quote is None else quote,
        }
        entry.update(extra)
        return entry

    def make_workspace(
        self,
        root: Path,
        *,
        grounding: list | None = None,
        sidecar=_DEFAULT_SIDECAR,
        declare_view: bool = True,
        hash_override: str | None = None,
    ) -> Path:
        target = root / "anchor-workspace"
        (target / "wiki" / "questions").mkdir(parents=True)
        normalized_dir = target / "sources" / "normalized"
        normalized_dir.mkdir(parents=True)
        (target / "research.yml").write_text("project:\n  name: Anchor Fixture\n", encoding="utf-8")

        payload = self.sidecar_document if sidecar is _DEFAULT_SIDECAR else sidecar
        declared_hash = hash_override
        if payload is not None:
            data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
            (normalized_dir / f"{self.safe_id}.structured.json").write_bytes(data)
            if declared_hash is None:
                declared_hash = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if declared_hash is None:
            declared_hash = f"sha256:{'0' * 64}"

        record = {
            "type": "normalized_source",
            "source_id": self.source_id,
            "title": "Keepa supplier quote",
        }
        if declare_view:
            record["structured_view"] = {
                "path": f"sources/normalized/{self.safe_id}.structured.json",
                "content_hash": declared_hash,
            }
        (normalized_dir / f"{self.safe_id}.md").write_text(
            "---\n"
            + yaml.safe_dump(record, sort_keys=False)
            + f"---\n\n# Keepa supplier quote\n\n{self.record_body}\n",
            encoding="utf-8",
        )

        question = {
            "type": "question",
            "status": "answered",
            "question": "What is the current supplier price?",
            "source_ids": [self.source_id],
            "answer_page": "../synthesis/supplier-price.md",
            "coverage_required": True,
            "answered_by": "answer-agent",
            "grounding": [self.anchor_entry()] if grounding is None else grounding,
        }
        (target / "wiki" / "questions" / f"{self.slug}.md").write_text(
            "---\n" + yaml.safe_dump(question, sort_keys=False) + "---\n\n# Supplier price\n",
            encoding="utf-8",
        )
        return target

    def run_verify(self, target: Path, *extra: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.main(["--project-root", str(target), "--slug", self.slug, *extra])
        payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}
        return int(code or 0), payload, stderr.getvalue()

    def question_page(self, target: Path) -> Path:
        return target / "wiki" / "questions" / f"{self.slug}.md"

    def question_frontmatter(self, target: Path) -> dict:
        text = self.question_page(target).read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def test_anchor_resolving_to_the_cited_value_verifies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        question = payload["questions"][0]
        result = question["grounding"][0]
        self.assertEqual("verified", result["result"], stderr)
        self.assertEqual("anchor", result["form"])
        self.assertEqual("structured_anchor_evidence", result["policy"])
        self.assertEqual("/supplier_quote/price", result["pointer"])
        self.assertEqual("23.99 EUR", result["expected"])
        self.assertEqual("23.99 EUR", result["resolved"])
        self.assertEqual(f"sources/normalized/{self.safe_id}.md", result["normalized_record"])
        self.assertEqual(f"sources/normalized/{self.safe_id}.structured.json", result["structured_view"])
        self.assertEqual(
            [
                f"sources/normalized/{self.safe_id}.md",
                f"sources/normalized/{self.safe_id}.structured.json",
            ],
            result["artifacts"],
        )
        self.assertTrue(result["message"])
        self.assertTrue(result["remediation"])
        self.assertTrue(question["all_verified"])
        self.assertEqual({"quote": 0, "anchor": 1}, question["by_form"])

    def test_every_anchor_failure_result_is_reachable(self):
        cases = [
            {
                "name": "value_mismatch",
                "entry": self.anchor_entry(expected="24.99 EUR"),
                "result": "anchor_value_mismatch",
                "resolved": "23.99 EUR",
            },
            {
                "name": "number_mismatch_against_unit_bearing_expected",
                "entry": self.anchor_entry(pointer="supplier_quote/amount", expected="23.99 EUR"),
                "result": "anchor_value_mismatch",
                "resolved": "23.99",
            },
            {
                "name": "pointer_missing_key",
                "entry": self.anchor_entry(pointer="supplier_quote/list_price"),
                "result": "anchor_pointer_not_found",
            },
            {
                "name": "pointer_past_end_of_array",
                "entry": self.anchor_entry(pointer="history/7/price"),
                "result": "anchor_pointer_not_found",
            },
            {
                "name": "pointer_through_a_scalar",
                "entry": self.anchor_entry(pointer="supplier_quote/price/currency"),
                "result": "anchor_pointer_not_found",
            },
            {
                "name": "target_is_an_object",
                "entry": self.anchor_entry(pointer="supplier_quote/breakdown", expected="19.99 EUR"),
                "result": "anchor_target_not_scalar",
            },
            {
                "name": "target_is_an_array",
                "entry": self.anchor_entry(pointer="history", expected="23.99 EUR"),
                "result": "anchor_target_not_scalar",
            },
            {
                "name": "record_declares_no_structured_view",
                "workspace": {"declare_view": False},
                "result": "structured_view_missing",
            },
            {
                "name": "declared_sidecar_absent",
                "workspace": {"sidecar": None},
                "result": "structured_view_missing",
            },
            {
                "name": "sidecar_bytes_do_not_match_the_binding",
                "workspace": {"hash_override": f"sha256:{'1' * 64}"},
                "result": "structured_view_corrupt",
            },
            {
                "name": "sidecar_is_not_one_json_object",
                "workspace": {"sidecar": [{"price": "23.99 EUR"}]},
                "result": "structured_view_corrupt",
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(
                    Path(tmpdir),
                    grounding=[case.get("entry", self.anchor_entry())],
                    **case.get("workspace", {}),
                )

                code, payload, stderr = self.run_verify(target)

                result = payload["questions"][0]["grounding"][0]
                self.assertEqual(1, code, stderr)
                self.assertEqual(case["result"], result["result"])
                self.assertEqual("anchor", result["form"])
                self.assertEqual("structured_anchor_evidence", result["policy"])
                self.assertTrue(result["message"])
                self.assertTrue(result["remediation"])
                self.assertEqual(case.get("resolved"), result["resolved"])
                self.assertFalse(payload["questions"][0]["all_verified"])

    def test_anchor_against_an_unnormalized_source_reuses_source_not_normalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(Path(tmpdir))
            (target / "sources" / "normalized" / f"{self.safe_id}.md").unlink()

            code, payload, stderr = self.run_verify(target)

        result = payload["questions"][0]["grounding"][0]
        self.assertEqual(1, code, stderr)
        self.assertEqual("source_not_normalized", result["result"])
        self.assertEqual("anchor", result["form"])
        self.assertTrue(result["remediation"])

    def test_canonical_equality_covers_every_scalar_type(self):
        cases = [
            {"name": "string_exact", "pointer": "supplier_quote/price", "expected": "23.99 EUR", "verified": True},
            {
                "name": "string_case_and_space_folded",
                "pointer": "supplier_quote/price",
                "expected": "23.99   eur",
                "verified": True,
            },
            {"name": "number_as_string", "pointer": "supplier_quote/amount", "expected": "23.99", "verified": True},
            {
                "name": "number_with_trailing_zero",
                "pointer": "supplier_quote/amount",
                "expected": "23.990",
                "verified": True,
            },
            {"name": "number_as_yaml_float", "pointer": "supplier_quote/amount", "expected": 23.99, "verified": True},
            {"name": "number_differs", "pointer": "supplier_quote/amount", "expected": "23.98", "verified": False},
            {"name": "boolean_as_yaml_bool", "pointer": "supplier_quote/in_stock", "expected": True, "verified": True},
            {"name": "boolean_as_string", "pointer": "supplier_quote/in_stock", "expected": "true", "verified": True},
            {"name": "boolean_differs", "pointer": "supplier_quote/in_stock", "expected": "false", "verified": False},
            {"name": "null_target", "pointer": "supplier_quote/discount", "expected": "null", "verified": True},
            {"name": "null_target_differs", "pointer": "supplier_quote/discount", "expected": "0", "verified": False},
            {"name": "array_step", "pointer": "history/1/price", "expected": "23.99 EUR", "verified": True},
            {"name": "leading_slash_form", "pointer": "/supplier_quote/price", "expected": "23.99 EUR", "verified": True},
            {"name": "escaped_solidus_token", "pointer": "odd~1key", "expected": "slash token", "verified": True},
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(
                    Path(tmpdir),
                    grounding=[self.anchor_entry(pointer=case["pointer"], expected=case["expected"])],
                )

                code, payload, stderr = self.run_verify(target)

                result = payload["questions"][0]["grounding"][0]
                self.assertEqual(0 if case["verified"] else 1, code, stderr)
                self.assertEqual("verified" if case["verified"] else "anchor_value_mismatch", result["result"])

    def test_anchor_verification_has_no_ambiguity_result(self):
        # Anchors compare one named field with one value. There is nothing to search, so
        # the quote path's `quote_ambiguous` has no analogue here — a value repeated
        # across the view cannot make the cited field ambiguous.
        self.assertNotIn(VERIFY.RESULT_QUOTE_AMBIGUOUS, VERIFY._structured_view.ANCHOR_RESULTS)
        repeated = {"first": {"price": "23.99 EUR"}, "second": {"price": "23.99 EUR"}}
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                sidecar=repeated,
                grounding=[self.anchor_entry(pointer="second/price")],
            )

            code, payload, stderr = self.run_verify(target)

        result = payload["questions"][0]["grounding"][0]
        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", result["result"])
        self.assertEqual("/second/price", result["pointer"])
        self.assertNotIn("occurrence_count", result)

    def test_mixed_quote_and_anchor_entries_verify_side_by_side(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                grounding=[self.quote_entry(), self.anchor_entry()],
            )

            code, payload, stderr = self.run_verify(target)

        self.assertEqual(0, code, stderr)
        question = payload["questions"][0]
        quote_result, anchor_result = question["grounding"]
        self.assertEqual(("quote", "verified"), (quote_result["form"], quote_result["result"]))
        self.assertEqual(("anchor", "verified"), (anchor_result["form"], anchor_result["result"]))
        self.assertEqual("retained_quote_evidence", quote_result["policy"])
        self.assertEqual("structured_anchor_evidence", anchor_result["policy"])
        self.assertEqual(2, question["grounding_count"])
        self.assertEqual({"quote": 1, "anchor": 1}, question["by_form"])
        self.assertEqual({"quote": 1, "anchor": 1}, payload["counts"]["by_form"])
        self.assertEqual(2, payload["counts"]["verified"])
        self.assertEqual(0, payload["counts"]["failed"])

    def test_render_text_names_the_form_of_every_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                grounding=[self.quote_entry(), self.anchor_entry()],
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = VERIFY.main(
                    ["--project-root", str(target), "--slug", self.slug, "--format", "text"]
                )
            rendered = stdout.getvalue()

        self.assertEqual(0, code, stderr.getvalue())
        self.assertIn("[quote]: verified", rendered)
        self.assertIn("[anchor]: verified", rendered)

    def test_write_stamps_a_mixed_question_only_when_both_forms_verify(self):
        cases = [
            {
                "name": "both_forms_verify",
                "grounding": [self.quote_entry(), self.anchor_entry()],
                "stamped": True,
            },
            {
                "name": "anchor_fails",
                "grounding": [self.quote_entry(), self.anchor_entry(expected="24.99 EUR")],
                "stamped": False,
                "error_code": "GROUNDING_ANCHOR_INVALID",
            },
            {
                "name": "quote_fails",
                "grounding": [self.quote_entry(quote="Never retained anywhere."), self.anchor_entry()],
                "stamped": False,
                "error_code": "GROUNDING_QUOTE_INVALID",
            },
            {
                "name": "both_forms_fail",
                "grounding": [
                    self.quote_entry(quote="Never retained anywhere."),
                    self.anchor_entry(expected="24.99 EUR"),
                ],
                "stamped": False,
                "error_code": "GROUNDING_ANCHOR_INVALID",
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(Path(tmpdir), grounding=case["grounding"])
                before = self.question_page(target).read_bytes()

                code, _, stderr = self.run_verify(target, "--write", "--verified-by", "verify-agent")

                frontmatter = self.question_frontmatter(target)
                after = self.question_page(target).read_bytes()
                if case["stamped"]:
                    self.assertEqual(0, code, stderr)
                    self.assertEqual("verify-agent", frontmatter["verified_by"])
                    self.assertIn("grounding_verified_at", frontmatter)
                else:
                    self.assertEqual(2, code, stderr)
                    envelope = json.loads(stderr)
                    self.assertEqual(case["error_code"], envelope["error_code"])
                    self.assertTrue(envelope["details"]["failures"])
                    self.assertNotIn("verified_by", frontmatter)
                    self.assertEqual(before, after)

    def test_failure_code_selection_is_exported_for_answer_time_callers(self):
        quote_failure = {"form": "quote", "result": "quote_not_found"}
        anchor_failure = {"form": "anchor", "result": "anchor_value_mismatch"}
        anchor_verified = {"form": "anchor", "result": "verified"}

        self.assertEqual("GROUNDING_QUOTE_INVALID", VERIFY.grounding_failure_error_code([]))
        self.assertEqual("GROUNDING_QUOTE_INVALID", VERIFY.grounding_failure_error_code([quote_failure]))
        self.assertEqual("GROUNDING_ANCHOR_INVALID", VERIFY.grounding_failure_error_code([anchor_failure]))
        self.assertEqual(
            "GROUNDING_ANCHOR_INVALID",
            VERIFY.grounding_failure_error_code([quote_failure, anchor_failure]),
        )
        # A verified anchor never chooses the code, so passing a whole grounding list and
        # passing only its failures agree.
        self.assertEqual(
            "GROUNDING_QUOTE_INVALID",
            VERIFY.grounding_failure_error_code([anchor_verified, quote_failure]),
        )

    def test_entry_shape_violations_are_fatal_and_name_the_entry(self):
        cases = [
            {
                "name": "both_forms",
                "entry": self.anchor_entry(quote="Supplier quote retained for the current period."),
            },
            {"name": "neither_form", "entry": {"claim": "A claim.", "source_id": self.source_id}},
            {"name": "location_hint_beside_anchor", "entry": self.anchor_entry(location_hint="page 2")},
            {"name": "unknown_anchor_key", "entry": self.anchor_entry(), "anchor_extra": {"line": 4}},
            {"name": "anchor_is_not_a_mapping", "entry": self.anchor_entry(), "anchor_replace": "supplier_quote/price"},
            {"name": "empty_pointer", "entry": self.anchor_entry(pointer="")},
            {"name": "whitespace_pointer", "entry": self.anchor_entry(pointer="   ")},
            {"name": "non_string_pointer", "entry": self.anchor_entry(pointer=7)},
            {"name": "missing_pointer", "entry": self.anchor_entry(), "anchor_drop": "pointer"},
            {"name": "missing_expected", "entry": self.anchor_entry(), "anchor_drop": "expected"},
            {"name": "null_expected", "entry": self.anchor_entry(expected=None)},
            {"name": "mapping_expected", "entry": self.anchor_entry(expected={"amount": "23.99"})},
            {"name": "sequence_expected", "entry": self.anchor_entry(expected=["23.99 EUR"])},
        ]
        for case in cases:
            entry = copy.deepcopy(case["entry"])
            if "anchor_extra" in case:
                entry["anchor"].update(case["anchor_extra"])
            if "anchor_replace" in case:
                entry["anchor"] = case["anchor_replace"]
            if "anchor_drop" in case:
                entry["anchor"].pop(case["anchor_drop"])
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmpdir:
                target = self.make_workspace(Path(tmpdir), grounding=[entry])

                code, payload, stderr = self.run_verify(target)

                self.assertEqual(2, code, stderr)
                self.assertEqual({}, payload)
                envelope = json.loads(stderr)
                self.assertEqual("GROUNDING_INVALID", envelope["error_code"])
                self.assertEqual(0, envelope["details"]["index"])
                self.assertEqual(self.slug, envelope["details"]["slug"])

    def test_expected_is_canonicalized_to_a_string_at_load(self):
        # The frontmatter writer refuses a non-string grounding scalar, so canonicalizing
        # here is what lets `expected: 23.99` survive a YAML round trip as one type.
        frontmatter = {
            "grounding": [
                self.anchor_entry(pointer="supplier_quote/amount", expected=23.99),
                self.anchor_entry(pointer="supplier_quote/in_stock", expected=True),
            ]
        }

        entries = VERIFY.grounding_entries(frontmatter, self.slug)

        self.assertEqual(["anchor", "anchor"], [entry["form"] for entry in entries])
        self.assertEqual("23.99", entries[0]["anchor"]["expected"])
        self.assertEqual("true", entries[1]["anchor"]["expected"])
        self.assertEqual("supplier_quote/amount", entries[0]["anchor"]["pointer"])

    def test_pointer_is_stored_as_written_and_reported_normalized(self):
        # RFC 6901 reference tokens may legitimately begin or end with a space, so the
        # stored pointer is never trimmed; only the report echoes the normalized form.
        frontmatter = {"grounding": [self.anchor_entry(pointer="supplier_quote/price")]}

        entries = VERIFY.grounding_entries(frontmatter, self.slug)
        self.assertEqual("supplier_quote/price", entries[0]["anchor"]["pointer"])

        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.make_workspace(
                Path(tmpdir),
                sidecar={"spaced key ": "kept verbatim"},
                grounding=[self.anchor_entry(pointer="/spaced key ", expected="kept verbatim")],
            )

            code, payload, stderr = self.run_verify(target)

        result = payload["questions"][0]["grounding"][0]
        self.assertEqual(0, code, stderr)
        self.assertEqual("verified", result["result"])
        self.assertEqual("/spaced key ", result["pointer"])


if __name__ == "__main__":
    unittest.main()
