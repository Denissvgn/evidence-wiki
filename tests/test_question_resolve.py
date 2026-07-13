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
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RESOLVE = load_script_module("research_question_resolve", "question_resolve.py")
CLAIM = load_script_module("research_question_resolve_claim", "question_claim.py")
INIT = load_script_module("research_question_resolve_init", "init_research_workspace.py")
REQUESTS = load_script_module("research_question_resolve_requests", "source_requests.py")
LINT = load_script_module("research_question_resolve_lint", "lint.py")
NORMALIZE = load_script_module("research_question_resolve_normalize", "normalize_sources.py")


class QuestionResolveTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        target = root / "resolve-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"},
            {"id": "needs-evidence", "question": "Needs missing evidence?", "priority": "medium"},
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def run_claim(self, target: Path, slug: str, agent_id: str = "agent-a") -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = CLAIM.main(
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    slug,
                    "--agent-id",
                    agent_id,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def run_resolve(self, target: Path, *args: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(["--project-root", str(target), *args, "--format", "json"])
        payload = json.loads(stdout.getvalue() or stderr.getvalue())
        return int(code or 0), payload, stderr.getvalue()

    def add_request(
        self,
        target: Path,
        slug: str = "needs-evidence",
        query_or_identifier: str = "arXiv:2601.00001",
    ) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = REQUESTS.main(
                [
                    "--project-root",
                    str(target),
                    "add",
                    "--kind",
                    "paper",
                    "--query-or-identifier",
                    query_or_identifier,
                    "--rationale",
                    "Blocks the question.",
                    "--question-slug",
                    slug,
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(0, code, stdout.getvalue())
        return json.loads(stdout.getvalue())["request"]["request_id"]

    def seed_manifest(self, target: Path, source_id: str = "raw:bench-survey-2026") -> None:
        record = {
            "id": source_id,
            "kind": "markdown",
            "raw_paths": ["raw/papers/bench-survey.md"],
            "status": "normalized",
            "detected_at": "2026-06-14T00:00:00Z",
        }
        (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    def seed_normalized_record(self, target: Path, source_id: str = "raw:bench-survey-2026") -> Path:
        config = NORMALIZE.load_config(target)
        _, normalized_rel = NORMALIZE.source_paths(config)
        normalized_dir = target / normalized_rel
        normalized_dir.mkdir(parents=True, exist_ok=True)
        record = normalized_dir / f"{NORMALIZE.safe_source_id(source_id)}.md"
        record.write_text(
            "---\n"
            "type: source\n"
            f"source_id: {source_id}\n"
            "title: Benchmark Survey 2026\n"
            "---\n\n"
            "# Benchmark Survey 2026\n\nNormalized content.\n",
            encoding="utf-8",
        )
        return record

    def set_question_grounding(
        self,
        target: Path,
        slug: str,
        *,
        quote: str = "Normalized content.",
        source_id: str = "raw:bench-survey-2026",
    ) -> None:
        question = target / "wiki" / "questions" / f"{slug}.md"
        text = question.read_text(encoding="utf-8")
        parts = text.split("---\n", 2)
        frontmatter = yaml.safe_load(parts[1])
        frontmatter["grounding"] = [
            {
                "claim": "Benchmarks are discussed in the survey.",
                "source_id": source_id,
                "quote": quote,
                "location_hint": "Benchmark Survey 2026",
            }
        ]
        question.write_text(
            "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + parts[2],
            encoding="utf-8",
        )

    def write_answer_page(self, target: Path) -> Path:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        answer = answer_dir / "benchmarks.md"
        answer.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-06-14\n"
            "updated: 2026-06-14\n"
            "source_ids: []\n"
            "summary: Benchmarks that matter.\n"
            "---\n\n"
            "# Benchmarks\n\nBody.\n",
            encoding="utf-8",
        )
        return answer

    def write_manual_review_coverage(self, target: Path, slug: str = "which-benchmarks") -> None:
        coverage = target / "sources" / "coverage" / f"{slug}.yml"
        coverage.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0",
                    "question_slug": slug,
                    "created_at": "2026-06-14T00:00:00Z",
                    "updated_at": "2026-06-14T00:00:00Z",
                    "coverage_profile": "manual-review-fixture",
                    "coverage_verdict": "pending",
                    "required_facets": [
                        {
                            "facet_id": "reviewed-evidence",
                            "description": "Require reviewer sign-off for this source.",
                            "required": True,
                            "evidence_path": "academic_method_existence",
                            "source_policy": "manual_review_required",
                            "freshness_policy": "no_staleness_check",
                            "identity_policy": "none",
                            "min_sources": 1,
                            "accepted_source_ids": ["raw:bench-survey-2026"],
                            "blocking_request_ids": [],
                            "facet_verdict": "pending",
                        }
                    ],
                    "optional_facets": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def page_frontmatter(self, target: Path, slug: str) -> dict:
        text = (target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def test_answer_resolves_claimed_question_with_citations_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            answer = self.write_answer_page(target)

            code, payload, stderr = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--confidence",
                "high",
                "--evidence-strength",
                "corroborated",
            )

            self.assertEqual(0, code, stderr)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["applied"])
            self.assertEqual("answered", payload["status"])
            self.assertEqual("wiki/questions/which-benchmarks.md", payload["question_page"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual("../synthesis/benchmarks.md", frontmatter["answer_page"])
            self.assertEqual(["raw:bench-survey-2026"], frontmatter["source_ids"])
            self.assertEqual("high", frontmatter["confidence"])
            self.assertEqual("corroborated", frontmatter["evidence_strength"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("claimed_at", frontmatter)
            self.assertIn("resolve | Question answered", (target / "log.md").read_text(encoding="utf-8"))

            results = LINT.run_checks(target, LINT.load_config(target))
            categories = {issue["category"] for issue in results["issues"]}
            self.assertNotIn("question_claim_missing", categories)
            self.assertNotIn("question_answer_missing", categories)

    def test_answer_without_source_id_is_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            answer = self.write_answer_page(target)
            question_path = target / "wiki" / "questions" / "which-benchmarks.md"
            before = question_path.read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
            )

            self.assertEqual(2, code)
            self.assertEqual("ANSWER_SOURCE_REQUIRED", payload["error_code"])
            self.assertEqual(before, question_path.read_text(encoding="utf-8"))
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("in_progress", frontmatter["status"])
            self.assertEqual("agent-a", frontmatter["claimed_by"])
            self.assertIn("claimed_at", frontmatter)

    def test_answer_allow_uncited_succeeds_without_source_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            answer = self.write_answer_page(target)

            code, payload, stderr = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--allow-uncited",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])
            self.assertEqual([], payload["source_ids"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual("../synthesis/benchmarks.md", frontmatter["answer_page"])
            self.assertEqual([], frontmatter["source_ids"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("claimed_at", frontmatter)

    def test_answer_requires_grounding_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            self.seed_normalized_record(target)
            answer = self.write_answer_page(target)
            question_path = target / "wiki" / "questions" / "which-benchmarks.md"
            before = question_path.read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--require-grounding",
            )

            self.assertEqual(2, code)
            self.assertEqual("GROUNDING_REQUIRED", payload["error_code"])
            self.assertEqual(before, question_path.read_text(encoding="utf-8"))

    def test_answer_require_grounding_refuses_missing_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            self.seed_normalized_record(target)
            self.set_question_grounding(target, "which-benchmarks", quote="Not present in the source.")
            answer = self.write_answer_page(target)

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--require-grounding",
            )

            self.assertEqual(2, code)
            self.assertEqual("GROUNDING_QUOTE_INVALID", payload["error_code"])
            self.assertEqual("in_progress", self.page_frontmatter(target, "which-benchmarks")["status"])

    def test_answer_require_grounding_succeeds_with_verified_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            self.seed_normalized_record(target)
            self.set_question_grounding(target, "which-benchmarks")
            answer = self.write_answer_page(target)

            code, payload, stderr = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--require-grounding",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertTrue(frontmatter["grounding_required"])
            self.assertEqual("agent-a", frontmatter["answered_by"])

    def test_answer_require_coverage_with_manual_review_policy_enters_human_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            self.write_manual_review_coverage(target)
            answer = self.write_answer_page(target)

            code, payload, stderr = self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--require-coverage",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("human_review", payload["status"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("human_review", frontmatter["status"])
            self.assertTrue(frontmatter["human_review_required"])
            self.assertEqual("pending", frontmatter["human_review_status"])
            self.assertEqual(["manual_review_required"], frontmatter["human_review_policies"])

    def test_approve_records_reviewer_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            self.write_manual_review_coverage(target)
            answer = self.write_answer_page(target)
            self.run_resolve(
                target,
                "answer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "raw:bench-survey-2026",
                "--require-coverage",
            )

            code, payload, stderr = self.run_resolve(
                target,
                "approve",
                "--slug",
                "which-benchmarks",
                "--reviewer",
                "reviewer-a",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])
            self.assertEqual("reviewer-a", payload["reviewer"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertTrue(frontmatter["human_review_approved"])
            self.assertEqual("approved", frontmatter["human_review_status"])
            self.assertEqual("reviewer-a", frontmatter["approved_by"])
            self.assertIn("approved_at", frontmatter)

    def test_block_requires_linked_request_and_clears_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "needs-evidence")
            request_id = self.add_request(target, "needs-evidence")

            code, payload, stderr = self.run_resolve(
                target,
                "block",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--blocked-reason",
                "Needs a benchmark report from a fetch agent.",
                "--request-id",
                request_id,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", payload["status"])
            self.assertEqual([request_id], payload["request_ids"])
            frontmatter = self.page_frontmatter(target, "needs-evidence")
            self.assertEqual("blocked", frontmatter["status"])
            self.assertEqual("Needs a benchmark report from a fetch agent.", frontmatter["blocked_reason"])
            self.assertEqual([request_id], frontmatter["blocking_request_ids"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("claimed_at", frontmatter)

    def test_block_merges_request_ids_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target, "needs-evidence")
            second = self.add_request(target, "needs-evidence", query_or_identifier="arXiv:2601.00002")
            question = target / "wiki" / "questions" / "needs-evidence.md"
            text = question.read_text(encoding="utf-8").replace(
                "source_ids: []",
                f"source_ids: []\nblocking_request_ids:\n  - {second}",
                1,
            )
            question.write_text(text, encoding="utf-8")
            self.run_claim(target, "needs-evidence")

            code, payload, stderr = self.run_resolve(
                target,
                "block",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--blocked-reason",
                "Needs multiple official sources.",
                "--request-id",
                first,
                "--request-id",
                second,
                "--request-id",
                first,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual([first, second], payload["request_ids"])
            frontmatter = self.page_frontmatter(target, "needs-evidence")
            self.assertEqual([second, first], frontmatter["blocking_request_ids"])

    def test_reopen_moves_blocked_question_to_open_with_normalized_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "needs-evidence")
            request_id = self.add_request(target, "needs-evidence")
            self.run_resolve(
                target,
                "block",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--blocked-reason",
                "Needs a fetched benchmark report.",
                "--request-id",
                request_id,
            )
            self.seed_manifest(target, "raw:bench-survey-2026")
            self.seed_normalized_record(target, "raw:bench-survey-2026")

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                "raw:bench-survey-2026",
                "--request-id",
                request_id,
            )

            self.assertEqual(0, code, stderr)
            self.assertTrue(payload["applied"])
            self.assertEqual("open", payload["status"])
            self.assertEqual(["raw:bench-survey-2026"], payload["source_ids"])
            self.assertEqual([request_id], payload["request_ids"])
            frontmatter = self.page_frontmatter(target, "needs-evidence")
            self.assertEqual("open", frontmatter["status"])
            self.assertNotIn("blocked_reason", frontmatter)
            self.assertNotIn("blocking_request_ids", frontmatter)
            self.assertEqual(["raw:bench-survey-2026"], frontmatter["source_ids"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertIn("resolve | Question reopened", (target / "log.md").read_text(encoding="utf-8"))

            # The reopened question is actionable again: it can be claimed and answered.
            self.run_claim(target, "needs-evidence", agent_id="agent-b")

    def test_reopen_refuses_non_blocked_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.seed_manifest(target, "raw:bench-survey-2026")
            self.seed_normalized_record(target, "raw:bench-survey-2026")

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                "raw:bench-survey-2026",
            )

            self.assertEqual(2, code)
            self.assertEqual("STATUS_NOT_REOPENABLE", payload["error_code"])

    def test_reopen_refuses_source_without_normalized_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "needs-evidence")
            request_id = self.add_request(target, "needs-evidence")
            self.run_resolve(
                target,
                "block",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--blocked-reason",
                "Needs a fetched benchmark report.",
                "--request-id",
                request_id,
            )
            # Manifest record exists but it was never normalized.
            self.seed_manifest(target, "raw:bench-survey-2026")

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                "raw:bench-survey-2026",
            )

            self.assertEqual(2, code)
            self.assertEqual("SOURCE_NOT_NORMALIZED", payload["error_code"])

    def test_reopen_refuses_source_not_in_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "needs-evidence")
            request_id = self.add_request(target, "needs-evidence")
            self.run_resolve(
                target,
                "block",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--blocked-reason",
                "Needs a fetched benchmark report.",
                "--request-id",
                request_id,
            )

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                "raw:does-not-exist",
            )

            self.assertEqual(2, code)
            self.assertEqual("SOURCE_UNKNOWN", payload["error_code"])

    def test_defer_and_reject_write_resolution_reason(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.run_claim(target, "needs-evidence")

            defer_code, defer_payload, _ = self.run_resolve(
                target,
                "defer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-a",
                "--reason",
                "Waiting for the next benchmark refresh.",
            )
            reject_code, reject_payload, _ = self.run_resolve(
                target,
                "reject",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--reason",
                "Superseded by a narrower parent-agent question.",
            )

            self.assertEqual(0, defer_code)
            self.assertEqual("deferred", defer_payload["status"])
            self.assertEqual(0, reject_code)
            self.assertEqual("rejected", reject_payload["status"])
            deferred = self.page_frontmatter(target, "which-benchmarks")
            rejected = self.page_frontmatter(target, "needs-evidence")
            self.assertEqual("Waiting for the next benchmark refresh.", deferred["resolution_reason"])
            self.assertEqual("Superseded by a narrower parent-agent question.", rejected["resolution_reason"])
            self.assertNotIn("claimed_by", deferred)
            self.assertNotIn("claimed_at", rejected)

    def test_wrong_agent_and_unclaimed_question_are_refused_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks", agent_id="agent-a")
            before = (target / "wiki" / "questions" / "which-benchmarks.md").read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "defer",
                "--slug",
                "which-benchmarks",
                "--agent-id",
                "agent-b",
                "--reason",
                "Not mine.",
            )

            self.assertEqual(3, code)
            self.assertEqual("CLAIM_HELD", payload["error_code"])
            self.assertEqual(before, (target / "wiki" / "questions" / "which-benchmarks.md").read_text(encoding="utf-8"))

            code, payload, _ = self.run_resolve(
                target,
                "reject",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--reason",
                "No claim yet.",
            )
            self.assertEqual(2, code)
            self.assertEqual("QUESTION_NOT_CLAIMED", payload["error_code"])

            code, payload, _ = self.run_resolve(
                target,
                "reject",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "agent-a",
                "--reason",
                "Out of scope.",
                "--allow-unclaimed",
            )
            self.assertEqual(0, code)
            self.assertEqual("rejected", payload["status"])

    def test_invalid_inputs_use_json_error_envelopes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            answer = self.write_answer_page(target)
            request_id = self.add_request(target, "needs-evidence")

            cases = [
                (
                    [
                        "answer",
                        "--slug",
                        "which-benchmarks",
                        "--agent-id",
                        "agent-a",
                        "--answer-page",
                        "wiki/synthesis/missing.md",
                        "--source-id",
                        "raw:bench-survey-2026",
                    ],
                    "ANSWER_PAGE_MISSING",
                ),
                (
                    [
                        "answer",
                        "--slug",
                        "which-benchmarks",
                        "--agent-id",
                        "agent-a",
                        "--answer-page",
                        "../outside.md",
                        "--source-id",
                        "raw:bench-survey-2026",
                    ],
                    "ANSWER_PAGE_INVALID",
                ),
                (
                    [
                        "answer",
                        "--slug",
                        "which-benchmarks",
                        "--agent-id",
                        "agent-a",
                        "--answer-page",
                        answer.relative_to(target).as_posix(),
                        "--source-id",
                        "raw:missing",
                    ],
                    "SOURCE_UNKNOWN",
                ),
                (
                    [
                        "block",
                        "--slug",
                        "which-benchmarks",
                        "--agent-id",
                        "agent-a",
                        "--blocked-reason",
                        "Needs evidence.",
                        "--request-id",
                        "req-missing",
                    ],
                    "REQUEST_UNKNOWN",
                ),
                (
                    [
                        "block",
                        "--slug",
                        "which-benchmarks",
                        "--agent-id",
                        "agent-a",
                        "--blocked-reason",
                        "Needs evidence.",
                        "--request-id",
                        request_id,
                    ],
                    "REQUEST_NOT_LINKED",
                ),
            ]
            for args, expected_code in cases:
                with self.subTest(expected_code=expected_code):
                    code, payload, _ = self.run_resolve(target, *args)
                    self.assertEqual(2, code)
                    self.assertEqual(expected_code, payload["error_code"])
                    self.assertIn("remediation", payload)


if __name__ == "__main__":
    unittest.main()
