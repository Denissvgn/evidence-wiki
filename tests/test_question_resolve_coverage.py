import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

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


RESOLVE = load_script_module("research_question_resolve_coverage", "question_resolve.py")
CLAIM = load_script_module("research_question_resolve_coverage_claim", "question_claim.py")
INIT = load_script_module("research_question_resolve_coverage_init", "init_research_workspace.py")


class QuestionResolveCoverageTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        target = root / "coverage-resolve-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "minimal-social-security-fee", "question": "What is the current reduced fee?", "priority": "high"},
            {"id": "turboquant-existence", "question": "Does TurboQuant exist?", "priority": "high"},
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "medium"},
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = INIT.main(["--profile", str(profile_path)])
        self.assertEqual(0, int(code or 0))
        return target

    def run_claim(self, target: Path, slug: str, agent_id: str = "agent-a") -> dict[str, Any]:
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
        self.assertEqual(0, int(code or 0), stdout.getvalue())
        return json.loads(stdout.getvalue())

    def run_resolve(self, target: Path, *args: str) -> tuple[int, dict[str, Any], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = RESOLVE.main(["--project-root", str(target), *args, "--format", "json"])
        payload = json.loads(stdout.getvalue() or stderr.getvalue())
        return int(code or 0), payload, stderr.getvalue()

    def seed_manifest_source(self, target: Path, source_id: str) -> None:
        record = {
            "id": source_id,
            "kind": "markdown",
            "raw_paths": ["raw/web/source.md"],
            "status": "normalized",
            "detected_at": "2026-06-29T00:00:00Z",
        }
        manifest = target / "sources" / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        existing = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
        manifest.write_text(existing + json.dumps(record) + "\n", encoding="utf-8")
        if source_id == "paper:turboquant":
            normalized = target / "sources" / "normalized" / "paper--turboquant.md"
            normalized.parent.mkdir(parents=True, exist_ok=True)
            normalized.write_text(
                "---\n"
                "type: normalized_source\n"
                "source_id: paper:turboquant\n"
                "status: normalized\n"
                "title: TurboQuant Synthetic Academic Fixture\n"
                "doi: 10.5555/turboquant\n"
                "publication_year: 2026\n"
                "authors:\n"
                "  - Ada Lovelace\n"
                "---\n\n"
                "# TurboQuant Synthetic Academic Fixture\n",
                encoding="utf-8",
            )

    def write_answer_page(self, target: Path, name: str = "answer.md") -> Path:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        answer = answer_dir / name
        answer.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-06-29\n"
            "updated: 2026-06-29\n"
            "source_ids: []\n"
            "summary: Covered answer.\n"
            "---\n\n"
            "# Covered Answer\n\nBody.\n",
            encoding="utf-8",
        )
        return answer

    def write_coverage_manifest(self, target: Path, slug: str, *, source_id: str | None, blocked: bool = False) -> Path:
        facet = {
            "facet_id": "required-identity",
            "description": "Required evidence facet.",
            "required": True,
            "evidence_path": "academic_method_existence",
            "source_policy": "academic_indexed",
            "freshness_policy": "publication_identity",
            "identity_policy": "citation_id_resolves",
            "min_sources": 1,
            "accepted_source_ids": [source_id] if source_id else [],
            "blocking_request_ids": ["req-missing-current-fee"] if blocked else [],
            "facet_verdict": "pending",
        }
        document = {
            "schema_version": "1.0",
            "question_slug": slug,
            "created_at": "2026-06-29T00:00:00Z",
            "updated_at": "2026-06-29T00:00:00Z",
            "coverage_profile": "academic-method-existence",
            "coverage_verdict": "pending",
            "required_facets": [facet],
            "optional_facets": [],
        }
        path = target / "sources" / "coverage" / f"{slug}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return path

    def question_text(self, target: Path, slug: str) -> str:
        return (target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")

    def question_frontmatter(self, target: Path, slug: str) -> dict[str, Any]:
        return yaml.safe_load(self.question_text(target, slug).split("---\n", 2)[1])

    def test_partial_coverage_refuses_answer_without_mutating_claimed_question(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "minimal-social-security-fee")
            self.seed_manifest_source(target, "web:seg-social-cuota-reducida-2026")
            self.write_coverage_manifest(target, "minimal-social-security-fee", source_id=None, blocked=True)
            answer = self.write_answer_page(target, "fee.md")
            before = self.question_text(target, "minimal-social-security-fee")

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "minimal-social-security-fee",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "web:seg-social-cuota-reducida-2026",
                "--require-coverage",
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_BLOCKED", payload["error_code"])
            self.assertEqual("sources/coverage/minimal-social-security-fee.yml", payload["details"]["manifest_path"])
            self.assertEqual("blocked", payload["details"]["coverage_verdict"])
            self.assertEqual(["required-identity"], payload["details"]["failed_required_facets"])
            self.assertEqual(before, self.question_text(target, "minimal-social-security-fee"))
            frontmatter = self.question_frontmatter(target, "minimal-social-security-fee")
            self.assertEqual("in_progress", frontmatter["status"])
            self.assertEqual("agent-a", frontmatter["claimed_by"])

    def test_complete_academic_coverage_allows_answer_resolution(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "turboquant-existence")
            self.seed_manifest_source(target, "paper:turboquant")
            self.write_coverage_manifest(target, "turboquant-existence", source_id="paper:turboquant")
            answer = self.write_answer_page(target, "turboquant.md")

            code, payload, stderr = self.run_resolve(
                target,
                "answer",
                "--slug",
                "turboquant-existence",
                "--agent-id",
                "agent-a",
                "--answer-page",
                answer.relative_to(target).as_posix(),
                "--source-id",
                "paper:turboquant",
                "--require-coverage",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])
            frontmatter = self.question_frontmatter(target, "turboquant-existence")
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual(["paper:turboquant"], frontmatter["source_ids"])
            self.assertIs(True, frontmatter["coverage_required"])
            self.assertEqual("sources/coverage/turboquant-existence.yml", frontmatter["coverage_manifest"])
            self.assertNotIn("claimed_by", frontmatter)

    def test_missing_required_manifest_returns_coverage_required(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest_source(target, "raw:bench-survey-2026")
            answer = self.write_answer_page(target, "benchmarks.md")

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
                "--require-coverage",
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_REQUIRED", payload["error_code"])
            self.assertEqual("sources/coverage/which-benchmarks.yml", payload["details"]["manifest_path"])

    def test_invalid_manifest_and_unsafe_override_return_manifest_invalid(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest_source(target, "raw:bench-survey-2026")
            answer = self.write_answer_page(target, "benchmarks.md")
            invalid = target / "sources" / "coverage" / "wrong-slug.yml"
            invalid.parent.mkdir(parents=True, exist_ok=True)
            invalid.write_text("schema_version: '1.0'\nquestion_slug: other-question\n", encoding="utf-8")

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
                "--require-coverage",
                "--coverage-manifest",
                "sources/coverage/wrong-slug.yml",
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_MANIFEST_INVALID", payload["error_code"])

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
                "--require-coverage",
                "--coverage-manifest",
                "../coverage.yml",
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_MANIFEST_INVALID", payload["error_code"])

    def test_source_id_only_answer_still_works_without_coverage_gate(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest_source(target, "raw:bench-survey-2026")
            answer = self.write_answer_page(target, "benchmarks.md")

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
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])

    def test_coverage_manifest_argument_without_require_coverage_does_not_gate(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest_source(target, "raw:bench-survey-2026")
            self.write_coverage_manifest(target, "which-benchmarks", source_id=None, blocked=True)
            answer = self.write_answer_page(target, "benchmarks.md")

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
                "--coverage-manifest",
                "sources/coverage/which-benchmarks.yml",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])


if __name__ == "__main__":
    unittest.main()
