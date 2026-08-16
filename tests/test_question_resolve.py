import ast
import contextlib
import importlib.util
import inspect
import io
import json
import sys
import tempfile
import unittest
from typing import NamedTuple
from pathlib import Path
from unittest import mock

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
INVENTORY = load_script_module("research_question_resolve_inventory", "source_inventory.py")


class _DoorBinding(NamedTuple):
    """One published in-process method and the packaged seam it forwards to."""

    label: str
    seam_module: object
    seam_name: str
    accepted: frozenset[str]
    forwarded: frozenset[str]


def _door_seam_bindings() -> list[_DoorBinding]:
    """Discover every in-process door method that forwards to a packaged seam.

    Two call shapes carry the same "every keyword mirrors the seam's" claim and both
    must be walked, because a guard that knows only one shape is blind to the other:

    - namespaces: ``self._call(STEM, "run_x", self._root, ...)``
    - the ``Workspace`` handle: ``call_seam(self._script, "stem", "run_x", root, ...)``

    Returns one binding per method, carrying the parameters the method *accepts*
    (positional-or-keyword and keyword-only, minus ``self``) and the keyword names it
    *forwards* in any call it makes.
    """
    sources = sorted((REPO_ROOT / "src" / "evidence_wiki" / "_facades").glob("*.py"))
    sources = [p for p in sources if p.name not in {"__init__.py", "_base.py"}]
    sources.append(REPO_ROOT / "src" / "evidence_wiki" / "workspace.py")

    bindings: list[_DoorBinding] = []
    seam_cache: dict[str, object] = {}
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }

        def literal(node: ast.expr) -> str | None:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                return consts.get(node.id)
            return None

        for method in ast.walk(tree):
            if not isinstance(method, ast.FunctionDef) or method.name.startswith("_"):
                continue
            stem = seam_name = None
            for call in ast.walk(method):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute) and call.func.attr == "_call" and len(call.args) >= 2:
                    stem, seam_name = literal(call.args[0]), literal(call.args[1])
                elif isinstance(call.func, ast.Name) and call.func.id == "call_seam" and len(call.args) >= 3:
                    stem, seam_name = literal(call.args[1]), literal(call.args[2])
                if stem and seam_name:
                    break
            if not (stem and seam_name):
                continue
            if stem not in seam_cache:
                script = SCRIPTS / f"{stem}.py"
                seam_cache[stem] = (
                    load_script_module(f"door_seam_{stem}", f"{stem}.py") if script.is_file() else None
                )
            module = seam_cache[stem]
            if module is None:
                continue
            accepted = {arg.arg for arg in method.args.args[1:]} | {
                arg.arg for arg in method.args.kwonlyargs
            }
            # A parameter counts as forwarded whether it is passed by keyword or
            # positionally: `grounding.verify` hands `slugs` on positionally, and reading
            # only `call.keywords` reported that as dropped. Same narrowness as reading
            # only `kwonlyargs` on the accepting side — a walk that knows one calling
            # convention is blind to the other.
            forwarded = {
                kw.arg
                for call in ast.walk(method)
                if isinstance(call, ast.Call)
                for kw in call.keywords
                if kw.arg
            } | {
                arg.id
                for call in ast.walk(method)
                if isinstance(call, ast.Call)
                for arg in call.args
                if isinstance(arg, ast.Name)
            }
            bindings.append(
                _DoorBinding(
                    label=f"{path.stem}.{method.name}",
                    seam_module=module,
                    seam_name=seam_name,
                    accepted=frozenset(accepted),
                    forwarded=frozenset(forwarded),
                )
            )
    return bindings


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

    def write_two_policy_manual_review_coverage(self, target: Path, slug: str = "which-benchmarks") -> list[str]:
        """Park the question behind one base and one pack-declared manual-review policy."""
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config["domain_pack"] = {
            "name": "market-data",
            "policy_vocabularies": {
                "freshness_policy": {
                    "pack:market-data/quote-48h": "Require a reviewer to confirm the quote is under 48 hours old.",
                }
            },
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        coverage = target / "sources" / "coverage" / f"{slug}.yml"
        coverage.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0",
                    "question_slug": slug,
                    "created_at": "2026-06-14T00:00:00Z",
                    "updated_at": "2026-06-14T00:00:00Z",
                    "coverage_profile": "two-policy-manual-review-fixture",
                    "coverage_verdict": "pending",
                    "required_facets": [
                        {
                            "facet_id": "reviewed-evidence",
                            "description": "Require reviewer sign-off for this source.",
                            "required": True,
                            "evidence_path": "academic_method_existence",
                            "source_policy": "manual_review_required",
                            "freshness_policy": "pack:market-data/quote-48h",
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
        return ["manual_review_required", "pack:market-data/quote-48h"]

    def park_for_review(self, target: Path, *, two_policies: bool = False) -> list[str]:
        """Answer the fixture question under coverage so it parks in human_review."""
        self.run_claim(target, "which-benchmarks")
        self.seed_manifest(target)
        if two_policies:
            policies = self.write_two_policy_manual_review_coverage(target)
        else:
            self.write_manual_review_coverage(target)
            policies = ["manual_review_required"]
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
        self.assertEqual(policies, sorted(self.page_frontmatter(target, "which-benchmarks")["human_review_policies"]))
        return policies

    def page_frontmatter(self, target: Path, slug: str) -> dict:
        text = (target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def test_answer_resolves_claimed_question_with_citations_and_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.run_claim(target, "which-benchmarks")
            self.seed_manifest(target)
            answer = self.write_answer_page(target)

            with mock.patch.object(RESOLVE.os.path, "relpath", return_value=r"..\synthesis\benchmarks.md"):
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

    def test_an_unknown_slug_is_refused_with_an_envelope_not_a_traceback(self):
        """`question_page_path` raises ClaimError, which main() did not catch.

        Every other refusal in this script reaches a host as a JSON envelope on stdout or
        stderr. An unhandled ClaimError reached it as a traceback instead — which, for a
        host parsing that stream as JSON, is indistinguishable from the process crashing.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "no-such-question",
                "--agent-id",
                "agent-a",
                "--answer-page",
                "wiki/synthesis/example.md",
                "--source-id",
                "raw:whatever",
            )

        self.assertEqual(2, code)
        self.assertEqual("SLUG_UNKNOWN", payload["error_code"])
        self.assertEqual("answer", payload["details"]["action"])
        self.assertEqual("no-such-question", payload["details"]["slug"])
        self.assertEqual("agent-a", payload["details"]["agent_id"])

    def test_a_slug_with_path_separators_is_refused_with_an_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, payload, _ = self.run_resolve(
                target,
                "answer",
                "--slug",
                "../escape",
                "--agent-id",
                "agent-a",
                "--answer-page",
                "wiki/synthesis/example.md",
                "--source-id",
                "raw:whatever",
            )

        self.assertEqual(2, code)
        self.assertEqual("SLUG_INVALID", payload["error_code"])

    def test_every_verb_refuses_an_unknown_slug_with_an_envelope(self):
        """The handler sits in main(), so it must cover every verb, nested one included."""
        verbs = (
            ("answer", "--answer-page", "wiki/synthesis/example.md", "--source-id", "raw:x"),
            ("block", "--blocked-reason", "why"),
            ("defer", "--reason", "why"),
            ("reject", "--reason", "why"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            results = {}
            for verb, *extra in verbs:
                results[verb] = self.run_resolve(
                    target, verb, "--slug", "no-such-question", "--agent-id", "agent-a", *extra
                )

        for verb, (code, payload, _) in results.items():
            with self.subTest(verb=verb):
                self.assertEqual(2, code)
                self.assertEqual("SLUG_UNKNOWN", payload["error_code"])

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

    def test_answer_stamps_human_review_requested_at_when_parking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target)

            frontmatter = self.page_frontmatter(target, "which-benchmarks")

            requested_at = frontmatter["human_review_requested_at"]
            self.assertIsInstance(requested_at, str)
            self.assertRegex(requested_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertNotIn("human_reviews", frontmatter)

    def test_review_accepting_one_of_two_policies_keeps_the_question_parked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)

            code, payload, stderr = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "pack:market-data/quote-48h",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
                "--review-ref",
                "approval-queue-42",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("human_review", payload["status"])
            self.assertEqual(["pack:market-data/quote-48h"], payload["reviewed_policies"])
            self.assertEqual(["manual_review_required"], payload["pending_policies"])
            self.assertEqual("approval-queue-42", payload["review_ref"])

            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("human_review", frontmatter["status"])
            self.assertEqual("pending", frontmatter["human_review_status"])
            self.assertNotIn("human_review_approved", frontmatter)
            self.assertEqual(1, len(frontmatter["human_reviews"]))
            self.assertEqual(
                {
                    "policy": "pack:market-data/quote-48h",
                    "verdict": "accepted",
                    "reviewed_by": "ops-principal",
                    "review_ref": "approval-queue-42",
                },
                {
                    key: value
                    for key, value in frontmatter["human_reviews"][0].items()
                    if key != "reviewed_at"
                },
            )
            self.assertRegex(frontmatter["human_reviews"][0]["reviewed_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_review_accepting_every_policy_answers_with_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)
            self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "pack:market-data/quote-48h",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
                "--review-ref",
                "approval-queue-42",
            )

            code, payload, stderr = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "reviewer-b",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("answered", payload["status"])
            self.assertEqual([], payload["pending_policies"])

            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertTrue(frontmatter["human_review_approved"])
            self.assertEqual("approved", frontmatter["human_review_status"])
            self.assertEqual("reviewer-b", frontmatter["approved_by"])
            self.assertIn("approved_at", frontmatter)
            self.assertEqual(
                ["pack:market-data/quote-48h", "manual_review_required"],
                [entry["policy"] for entry in frontmatter["human_reviews"]],
            )
            self.assertEqual(
                ["ops-principal", "reviewer-b"],
                [entry["reviewed_by"] for entry in frontmatter["human_reviews"]],
            )

    def test_review_rejection_reopens_the_question_and_retains_the_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target)

            code, payload, stderr = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "rejected",
                "--reviewed-by",
                "ops-principal",
                "--note",
                "The cited survey predates the reporting window.",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])

            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("open", frontmatter["status"])
            self.assertEqual("rejected", frontmatter["human_review_status"])
            self.assertNotIn("human_review_approved", frontmatter)
            self.assertNotIn("approved_by", frontmatter)
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("human_review_requested_at", frontmatter)
            self.assertNotIn("blocked_reason", frontmatter)
            entry = frontmatter["human_reviews"][0]
            self.assertEqual("rejected", entry["verdict"])
            self.assertEqual("The cited survey predates the reporting window.", entry["note"])

    def test_re_answer_after_rejection_reparks_with_a_fresh_review_cycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target)
            self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "rejected",
                "--reviewed-by",
                "ops-principal",
                "--note",
                "Needs a newer survey.",
            )
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
                "--allow-unclaimed",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("human_review", payload["status"])

            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("pending", frontmatter["human_review_status"])
            self.assertIn("human_review_requested_at", frontmatter)
            # A new answer opens a new review cycle: the superseded rejection must not linger
            # where a completion check could read it.
            self.assertNotIn("human_reviews", frontmatter)

    def test_review_refuses_unknown_policy_wrong_status_and_bad_verdict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target)

            code, error, _ = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "pack:other/not-declared",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
            )
            self.assertEqual(RESOLVE.EXIT_INVALID, code)
            self.assertEqual("REVIEW_POLICY_UNKNOWN", error["error_code"])

            code, error, _ = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "approved",
                "--reviewed-by",
                "ops-principal",
            )
            self.assertEqual(RESOLVE.EXIT_INVALID, code)
            self.assertEqual("REVIEW_VERDICT_INVALID", error["error_code"])

            code, error, _ = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "   ",
            )
            self.assertEqual(RESOLVE.EXIT_INVALID, code)
            self.assertEqual("REVIEWER_INVALID", error["error_code"])

            code, error, _ = self.run_resolve(
                target,
                "review",
                "--slug",
                "needs-evidence",
                "--policy",
                "manual_review_required",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
            )
            self.assertEqual(RESOLVE.EXIT_INVALID, code)
            self.assertEqual("STATUS_NOT_REVIEWABLE", error["error_code"])

    def test_review_refuses_a_second_accepted_review_for_one_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)
            self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
            )

            code, error, _ = self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "manual_review_required",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "someone-else",
            )

            self.assertEqual(RESOLVE.EXIT_INVALID, code)
            self.assertEqual("REVIEW_ALREADY_RECORDED", error["error_code"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual(1, len(frontmatter["human_reviews"]))

    def test_approve_records_an_entry_for_every_pending_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)

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
            self.assertEqual([], payload["pending_policies"])

            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual(
                ["manual_review_required", "pack:market-data/quote-48h"],
                sorted(entry["policy"] for entry in frontmatter["human_reviews"]),
            )
            for entry in frontmatter["human_reviews"]:
                self.assertEqual("accepted", entry["verdict"])
                self.assertEqual("reviewer-a", entry["reviewed_by"])
                self.assertNotIn("review_ref", entry)

    def test_approve_completes_a_partially_reviewed_question(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)
            self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "pack:market-data/quote-48h",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
                "--review-ref",
                "approval-queue-42",
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
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual(2, len(frontmatter["human_reviews"]))
            self.assertEqual("approval-queue-42", frontmatter["human_reviews"][0]["review_ref"])
            self.assertEqual("reviewer-a", frontmatter["human_reviews"][1]["reviewed_by"])

    def test_review_appends_a_log_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.park_for_review(target, two_policies=True)

            self.run_resolve(
                target,
                "review",
                "--slug",
                "which-benchmarks",
                "--policy",
                "pack:market-data/quote-48h",
                "--verdict",
                "accepted",
                "--reviewed-by",
                "ops-principal",
                "--review-ref",
                "approval-queue-42",
            )

            log = (target / "log.md").read_text(encoding="utf-8")
            self.assertIn("- Question: `which-benchmarks` (review).", log)
            self.assertIn("- Reviewer: ops-principal.", log)
            self.assertIn("- Reviewed accepted: pack:market-data/quote-48h.", log)
            self.assertIn("- Review reference: approval-queue-42.", log)
            self.assertIn("- Still pending review: manual_review_required.", log)

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

    # -- CR-4 T6: scope-based request -> source pairing on reopen -------------------
    #
    # Delivery is exercised through the real chain (raw file + .provenance.yml sidecar
    # -> source_inventory.py -> normalize_sources.py) rather than a hand-written
    # manifest, because the sidecar `scope` reaching `provenance.scope` on the manifest
    # record is half of what these tests are asserting.

    def deliver_scoped_source(self, target: Path, name: str, scope: dict | None) -> None:
        """Write one delivered raw file plus its provenance sidecar; no inventory yet."""
        destination = target / "raw" / "papers"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / f"{name}.html").write_text(
            f"<html><head><title>{name}</title></head><body><h1>{name}</h1>"
            f"<p>Delivered evidence for {name}. It states the measured value plainly.</p>"
            "</body></html>\n",
            encoding="utf-8",
        )
        sidecar = {
            "origin_url": f"https://example.test/{name}",
            "license": "CC-BY-4.0",
            "retrieved_at": "2026-08-09T12:00:00Z",
            "retrieved_by": "fetch-agent/manual-web",
        }
        if scope is not None:
            sidecar["scope"] = scope
        (destination / f"{name}.html.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
        )

    def inventory_and_normalize(self, target: Path) -> None:
        for module, args in ((INVENTORY, ["--report"]), (NORMALIZE, ["--all"])):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.main(["--project-root", str(target), *args])
            self.assertEqual(0, code or 0, stdout.getvalue() + stderr.getvalue())

    def source_id_for(self, target: Path, raw_path: str) -> str:
        for line in (target / "sources" / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if raw_path in record.get("raw_paths", []):
                return str(record["id"])
        raise AssertionError(f"no manifest record for {raw_path}")

    def set_request_scope(self, target: Path, request_id: str, scope: dict) -> None:
        """Stamp a structured scope onto an existing request record.

        ``source_requests.py add --scope`` is a sibling CR-4 unit; the record shape is
        the contract between them, so these tests write the field directly rather than
        depending on the flag's landing order.
        """
        path = target / "sources" / "source-requests.jsonl"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("request_id") == request_id:
                record["scope"] = scope
            lines.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def block_on_requests(self, target: Path, slug: str, request_ids: list[str]) -> None:
        self.run_claim(target, slug)
        args = ["block", "--slug", slug, "--agent-id", "agent-a", "--blocked-reason", "Needs delivered evidence."]
        for request_id in request_ids:
            args.extend(["--request-id", request_id])
        code, _, stderr = self.run_resolve(target, *args)
        self.assertEqual(0, code, stderr)

    def two_scoped_requests_blocked(self, target: Path) -> tuple[str, str]:
        heat = self.add_request(target, "needs-evidence", query_or_identifier="Heat index readings 2026")
        shade = self.add_request(target, "needs-evidence", query_or_identifier="Shade cover survey 2026")
        self.set_request_scope(target, heat, {"facet_id": "heat-index"})
        self.set_request_scope(target, shade, {"facet_id": "shade-cover"})
        self.block_on_requests(target, "needs-evidence", [heat, shade])
        return heat, shade

    def test_reopen_pairs_scoped_requests_with_matching_sources_in_any_order(self):
        """The CR's literal acceptance criterion: pairing is semantic, not positional."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            heat_request, shade_request = self.two_scoped_requests_blocked(target)
            self.deliver_scoped_source(target, "heat-index", {"facet_id": "heat-index"})
            self.deliver_scoped_source(target, "shade-cover", {"facet_id": "shade-cover"})
            self.inventory_and_normalize(target)
            heat_source = self.source_id_for(target, "raw/papers/heat-index.html")
            shade_source = self.source_id_for(target, "raw/papers/shade-cover.html")

            # Sources and requests are supplied in deliberately mismatched positional
            # order: zipping the two lists would pair heat with shade and vice versa.
            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                shade_source,
                "--source-id",
                heat_source,
                "--request-id",
                heat_request,
                "--request-id",
                shade_request,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])
            self.assertEqual(
                [
                    {"request_id": heat_request, "source_id": heat_source, "decided_by": "scope"},
                    {"request_id": shade_request, "source_id": shade_source, "decided_by": "scope"},
                ],
                payload["pairs"],
            )
            # Distinct facets: each request has exactly one compatible source, so scope
            # forced both pairs and there is nothing to warn about.
            self.assertEqual([], payload["warnings"])
            self.assertEqual("open", self.page_frontmatter(target, "needs-evidence")["status"])
            log = (target / "log.md").read_text(encoding="utf-8")
            self.assertIn(f"{heat_request} -> {heat_source}", log)
            self.assertIn(f"{shade_request} -> {shade_source}", log)
            self.assertIn("Paired by declared scope", log)
            self.assertNotIn("could not single out", log)

    def test_reopen_refuses_source_contradicting_the_scoped_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, "needs-evidence", query_or_identifier="Heat index readings 2026")
            self.set_request_scope(target, request_id, {"facet_id": "heat-index"})
            self.block_on_requests(target, "needs-evidence", [request_id])
            self.deliver_scoped_source(target, "shade-cover", {"facet_id": "shade-cover"})
            self.inventory_and_normalize(target)
            wrong_source = self.source_id_for(target, "raw/papers/shade-cover.html")
            before = (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                wrong_source,
                "--request-id",
                request_id,
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_MISMATCH", payload["error_code"])
            details = payload["details"]
            self.assertEqual("no_matching_source", details["reason"])
            self.assertEqual(request_id, details["request_id"])
            self.assertEqual({"facet_id": "heat-index"}, details["request_scope"])
            self.assertEqual(
                [
                    {
                        "source_id": wrong_source,
                        "conflicts": [
                            {"key": "facet_id", "request_value": "heat-index", "source_value": "shade-cover"}
                        ],
                    }
                ],
                details["rejected_sources"],
            )
            self.assertIn("facet_id", payload["message"])
            self.assertIn("heat-index", payload["message"])
            self.assertIn("shade-cover", payload["message"])
            self.assertIn("remediation", payload)
            self.assertEqual("blocked", self.page_frontmatter(target, "needs-evidence")["status"])
            self.assertEqual(before, (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8"))

    def test_reopen_refuses_two_scoped_requests_competing_for_one_source(self):
        """One source cannot answer two scoped requests: the assignment is ambiguous."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            heat_request, shade_request = self.two_scoped_requests_blocked(target)
            # A single delivery that contradicts neither request: absence is compatible,
            # so it is a candidate for both — and therefore proof of neither.
            self.deliver_scoped_source(target, "combined-survey", None)
            self.inventory_and_normalize(target)
            only_source = self.source_id_for(target, "raw/papers/combined-survey.html")
            before = (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                only_source,
                "--request-id",
                heat_request,
                "--request-id",
                shade_request,
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_MISMATCH", payload["error_code"])
            details = payload["details"]
            self.assertEqual("ambiguous_assignment", details["reason"])
            self.assertEqual(sorted([heat_request, shade_request]), details["request_ids"])
            self.assertEqual([only_source], details["source_ids"])
            self.assertEqual(
                [[only_source], [only_source]],
                [entry["candidate_source_ids"] for entry in details["requests"]],
            )
            self.assertEqual("blocked", self.page_frontmatter(target, "needs-evidence")["status"])
            self.assertEqual(before, (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8"))

    def test_reopen_leaves_scope_less_requests_unpaired(self):
        """Nothing declares scope, so nothing pairs and reopen behaves exactly as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target, "needs-evidence", query_or_identifier="Heat index readings 2026")
            second = self.add_request(target, "needs-evidence", query_or_identifier="Shade cover survey 2026")
            self.block_on_requests(target, "needs-evidence", [first, second])
            self.deliver_scoped_source(target, "combined-survey", None)
            self.inventory_and_normalize(target)
            source_id = self.source_id_for(target, "raw/papers/combined-survey.html")

            def refuse_lookup(source_id_value: str) -> dict:
                raise AssertionError(f"pairing read provenance scope for {source_id_value}")

            # A workspace where nothing declares scope must not pay for pairing at all:
            # no manifest record is read for a provenance scope that cannot matter.
            with mock.patch.object(RESOLVE, "source_scope_resolver", return_value=refuse_lookup):
                code, payload, stderr = self.run_resolve(
                    target,
                    "reopen",
                    "--slug",
                    "needs-evidence",
                    "--agent-id",
                    "fetch-agent",
                    "--source-id",
                    source_id,
                    "--request-id",
                    first,
                    "--request-id",
                    second,
                )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])
            self.assertEqual([], payload["pairs"])
            self.assertEqual([first, second], payload["request_ids"])
            self.assertNotIn("Paired by declared scope", (target / "log.md").read_text(encoding="utf-8"))

    def test_reopen_pairs_a_scoped_request_and_ignores_its_scope_less_sibling(self):
        """A partially adopted workspace: only the scoped request gets a pair."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            scoped = self.add_request(target, "needs-evidence", query_or_identifier="Heat index readings 2026")
            unscoped = self.add_request(target, "needs-evidence", query_or_identifier="Shade cover survey 2026")
            self.set_request_scope(target, scoped, {"facet_id": "heat-index"})
            self.block_on_requests(target, "needs-evidence", [scoped, unscoped])
            self.deliver_scoped_source(target, "heat-index", {"facet_id": "heat-index"})
            self.deliver_scoped_source(target, "combined-survey", None)
            self.inventory_and_normalize(target)
            heat_source = self.source_id_for(target, "raw/papers/heat-index.html")
            other_source = self.source_id_for(target, "raw/papers/combined-survey.html")

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                other_source,
                "--source-id",
                heat_source,
                "--request-id",
                scoped,
                "--request-id",
                unscoped,
            )

            self.assertEqual(0, code, stderr)
            # The unstamped source does not contradict the scoped request, but the stamped
            # one corroborates it, so the pairing names the source that actually agrees.
            self.assertEqual(
                [{"request_id": scoped, "source_id": heat_source, "decided_by": "scope"}],
                payload["pairs"],
            )
            # Corroboration outranking mere compatibility is a scope decision, not a tie:
            # warning here would fire on every partially stamped workspace.
            self.assertEqual([], payload["warnings"])

    def same_facet_requests_blocked(self, target: Path, count: int) -> list[str]:
        """Block one question on ``count`` requests that all declare the same facet.

        The shape a per-question scope key produces: scope narrows every request to the
        same candidate set, so it cannot say which delivery answers which request.
        """
        request_ids = [
            self.add_request(target, "needs-evidence", query_or_identifier=f"Supplier quote {index} 2026")
            for index in range(1, count + 1)
        ]
        for request_id in request_ids:
            self.set_request_scope(target, request_id, {"facet_id": "supplier-quote"})
        self.block_on_requests(target, "needs-evidence", request_ids)
        return request_ids

    def test_reopen_reports_a_tie_when_scope_cannot_single_out_a_source(self):
        """Non-discriminating scope pairs positionally; saying so is the whole point.

        Two requests on one question declaring the same facet, answered by two
        identically stamped deliveries: every source is compatible with every request,
        so the assignment comes from --source-id order. Reopen still pairs — it reports
        a pairing rather than recording a fulfilment — but must not credit scope for it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first, second = self.same_facet_requests_blocked(target, 2)
            self.deliver_scoped_source(target, "quote-one", {"facet_id": "supplier-quote"})
            self.deliver_scoped_source(target, "quote-two", {"facet_id": "supplier-quote"})
            self.inventory_and_normalize(target)
            one = self.source_id_for(target, "raw/papers/quote-one.html")
            two = self.source_id_for(target, "raw/papers/quote-two.html")

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                one,
                "--source-id",
                two,
                "--request-id",
                first,
                "--request-id",
                second,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])
            self.assertEqual(
                ["tie_break", "tie_break"],
                [pair["decided_by"] for pair in payload["pairs"]],
            )
            self.assertEqual(
                ["request_scope_pairing_tie", "request_scope_pairing_tie"],
                [warning["code"] for warning in payload["warnings"]],
            )
            self.assertEqual([first, second], [warning["request_id"] for warning in payload["warnings"]])
            # Both deliveries are used, and each request's alternative is the other one:
            # which request got which is exactly what the declared scope failed to say.
            assigned = {pair["request_id"]: pair["source_id"] for pair in payload["pairs"]}
            self.assertEqual(sorted([one, two]), sorted(assigned.values()))
            for warning in payload["warnings"]:
                other = two if assigned[warning["request_id"]] == one else one
                self.assertEqual([other], warning["alternative_source_ids"])
            self.assertIn("not by declared scope", payload["warnings"][0]["message"])

            # The audit entry is the thing that must not lie: no pair here was decided by
            # declared scope, so no line may claim one was.
            log = (target / "log.md").read_text(encoding="utf-8")
            self.assertNotIn("Paired by declared scope", log)
            self.assertIn("could not single out a source", log)
            for request_id, source_id in assigned.items():
                self.assertIn(f"{request_id} -> {source_id}", log)

    def test_reopen_reports_a_tie_when_another_request_could_have_taken_the_source(self):
        """Scope is symmetric between identically scoped requests, so both pairs are ties.

        Two requests declaring the same facet, one stamped delivery and one unstamped.
        The request that ends up with the stamped source has no better alternative of its
        own — but which of the two got it was decided by supply order, since scope cannot
        tell them apart at all. Asking only "could another source have answered this
        request" marks that pair `scope` and writes the claim into log.md.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first, second = self.same_facet_requests_blocked(target, 2)
            self.deliver_scoped_source(target, "quote-one", {"facet_id": "supplier-quote"})
            self.deliver_scoped_source(target, "quote-bare", None)
            self.inventory_and_normalize(target)
            stamped = self.source_id_for(target, "raw/papers/quote-one.html")
            bare = self.source_id_for(target, "raw/papers/quote-bare.html")

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                stamped,
                "--source-id",
                bare,
                "--request-id",
                first,
                "--request-id",
                second,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual(
                ["tie_break", "tie_break"],
                [pair["decided_by"] for pair in payload["pairs"]],
            )
            # The pair holding the stamped source is disclosed through the other axis:
            # no better source for it, but a request that could equally have had it.
            holder = next(pair for pair in payload["pairs"] if pair["source_id"] == stamped)
            other = next(pair for pair in payload["pairs"] if pair["source_id"] == bare)
            holder_warning = next(w for w in payload["warnings"] if w["request_id"] == holder["request_id"])
            self.assertEqual([], holder_warning["alternative_source_ids"])
            self.assertEqual([other["request_id"]], holder_warning["contending_request_ids"])
            self.assertIn("could equally have answered", holder_warning["message"])
            self.assertNotIn("Paired by declared scope", (target / "log.md").read_text(encoding="utf-8"))

    def test_reopen_tie_disclosure_accounts_for_reassignment_by_augmenting_path(self):
        """A pair's honesty is a property of the whole assignment, not of its own turn.

        Three same-facet requests over two stamped sources and one unstamped one force
        Kuhn's algorithm to move an already-assigned request onto a different source. A
        `decided_by` read off each request's candidate list, or off the order requests
        were placed, would misreport at least one of these; every pair here could have
        come out differently, so every pair is a tie.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first, second, third = self.same_facet_requests_blocked(target, 3)
            self.deliver_scoped_source(target, "quote-one", {"facet_id": "supplier-quote"})
            self.deliver_scoped_source(target, "quote-two", {"facet_id": "supplier-quote"})
            # Unstamped: compatible with every request, corroborating none of them.
            self.deliver_scoped_source(target, "quote-three", None)
            self.inventory_and_normalize(target)
            sources = [
                self.source_id_for(target, f"raw/papers/quote-{name}.html")
                for name in ("one", "two", "three")
            ]

            args = ["reopen", "--slug", "needs-evidence", "--agent-id", "fetch-agent"]
            for source_id in sources:
                args.extend(["--source-id", source_id])
            for request_id in (first, second, third):
                args.extend(["--request-id", request_id])
            code, payload, stderr = self.run_resolve(target, *args)

            self.assertEqual(0, code, stderr)
            self.assertEqual([first, second, third], [pair["request_id"] for pair in payload["pairs"]])
            self.assertEqual(sorted(sources), sorted(pair["source_id"] for pair in payload["pairs"]))
            self.assertEqual(
                ["tie_break", "tie_break", "tie_break"],
                [pair["decided_by"] for pair in payload["pairs"]],
            )
            self.assertEqual(3, len(payload["warnings"]))
            # The request left holding the unstamped source was not placed there by scope:
            # it was denied a corroborating source another request took first.
            unstamped_pair = next(pair for pair in payload["pairs"] if pair["source_id"] == sources[2])
            unstamped_warning = next(
                warning for warning in payload["warnings"] if warning["request_id"] == unstamped_pair["request_id"]
            )
            self.assertEqual(sorted(sources[:2]), sorted(unstamped_warning["alternative_source_ids"]))

    def test_dispatch_seam_forwards_every_cli_flag_to_its_seam(self):
        """Every parsed flag must reach the seam, or the CLI silently ignores it.

        `dispatch_seam` maps args to seams keyword by keyword, and its own docstring
        claims "a seam whose keyword does not reach the transition fails loudly here."
        It did not: adding `--require-decisive-scope` to the parser and to `run_reopen`
        left the CLI branch unchanged, so the flag parsed, the seam took its default, and
        the reopen succeeded when it should have refused — a dead flag, visible only
        because a CLI-level test happened to assert the refusal.

        Structural rather than behavioural on purpose: a per-flag behaviour test only
        covers the flags someone thought to exercise, which is the same blind spot.
        """
        source = Path(RESOLVE.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Branch keys are not all string literals — `grounding` dispatches on the module
        # constant GROUNDING_COMMAND. Resolving names against the module keeps that branch
        # inside the guard; matching only `ast.Constant` silently dropped it, which left
        # the one subcommand written that way unprotected by the very test meant to protect
        # it. Any future branch keyed on a constant is covered by the same resolution.
        def branch_command(test: ast.expr) -> str | None:
            if not (isinstance(test, ast.Compare) and test.comparators):
                return None
            node = test.comparators[0]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return node.value
            if isinstance(node, ast.Name):
                resolved = getattr(RESOLVE, node.id, None)
                return resolved if isinstance(resolved, str) else None
            return None

        seam_keywords: dict[str, set[str]] = {}
        dispatch = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "dispatch_seam"
        )
        for branch in ast.walk(dispatch):
            if not isinstance(branch, ast.If):
                continue
            command = branch_command(branch.test)
            if command is None:
                continue
            for call in (node for node in ast.walk(branch) if isinstance(node, ast.Call)):
                keywords = {kw.arg for kw in call.keywords if kw.arg}
                if keywords:
                    seam_keywords.setdefault(command, set()).update(keywords)

        # Not seam arguments: the dispatch keys themselves, the root passed positionally,
        # and the output format, which `main` applies to the seam's report rather than
        # passing on.
        not_forwarded = {"command", "grounding_command", "project_root", "format"}
        minimal = {
            "reopen": ["--slug", "s", "--agent-id", "a", "--source-id", "x"],
            "block": ["--slug", "s", "--agent-id", "a", "--blocked-reason", "r"],
            "defer": ["--slug", "s", "--agent-id", "a", "--reason", "r"],
            "reject": ["--slug", "s", "--agent-id", "a", "--reason", "r"],
            "approve": ["--slug", "s", "--reviewer", "r"],
            "answer": ["--slug", "s", "--agent-id", "a", "--answer-page", "p", "--allow-uncited"],
            "review": [
                "--slug", "s", "--policy", "p", "--verdict", "accepted", "--reviewed-by", "r",
            ],
            "grounding": ["set", "--slug", "s", "--agent-id", "a", "--from-file", "f"],
        }
        # Every registered subcommand must appear above. Without this, a new subcommand is
        # simply absent from the loop and unchecked, with nothing to prompt anyone — the
        # second half of how `grounding` escaped.
        registered = {
            call.args[0].value if isinstance(call.args[0], ast.Constant) else getattr(RESOLVE, call.args[0].id, None)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "add_parser"
            and call.args
            and isinstance(call.args[0], (ast.Constant, ast.Name))
        }
        top_level = {name for name in registered if isinstance(name, str)} - {"set"}
        self.assertEqual(
            set(), top_level - set(minimal), "these subcommands are registered but not covered by this guard"
        )
        self.assertTrue(seam_keywords, "no dispatch branches were parsed; the AST walk is wrong")
        for command, argv in minimal.items():
            parsed = set(vars(RESOLVE.parse_args([command, *argv]))) - not_forwarded
            dropped = sorted(parsed - seam_keywords.get(command, set()))
            self.assertEqual([], dropped, f"{command} parses these flags but never forwards them: {dropped}")

    def test_library_facade_forwards_every_seam_keyword(self):
        """The seam→facade boundary, which is where the CLI↔seam guard stops looking.

        `docs/library-api.md` promises a host can move an operation between the CLI and
        the in-process door "without changing what the operation means". Nothing enforced
        it: `--require-decisive-scope` reached the parser, `dispatch_seam` and
        `run_reopen`, and still could not be asked for through `questions.reopen`, so the
        documented doors disagreed about whether a tie-broken reopen is refused.

        Coverage is **derived, not listed**. The first version of this guard hardcoded a
        pair list for `QuestionsNamespace` — the namespace that had just broken — and so
        protected exactly the instance that bit, leaving six other namespaces and the
        `Workspace` handle carrying the same claim unguarded. A guard built from an
        instance protects that instance; this one walks the doors and finds its own work.

        The divergences below are the ones `library-api.md` calls deliberate; anything
        else missing from a door method is a flag that door cannot reach.
        """
        deliberate = {
            # A blocked question is never claimed, so reopen has no claim override.
            ("questions.reopen", "allow_unclaimed"),
            # approve/review record a reviewer principal rather than an agent id.
            ("questions.approve", "agent_id"),
            ("questions.review", "agent_id"),
            # `run_doctor(env=...)` injects a DoctorEnvironment to stub interpreter version
            # and optional imports. It is a test seam with no CLI flag behind it, so there
            # is no door to mirror; publishing it would put a test hook on the host API.
            ("diagnostics.doctor", "env"),
        }
        bindings = _door_seam_bindings()
        # Guard the guard: a walk that silently found nothing would pass forever, and the
        # binding that actually broke must be inside it.
        self.assertGreater(len(bindings), 10, "door→seam walk found suspiciously few bindings")
        self.assertIn("questions.reopen", {b.label for b in bindings})

        unreachable, dropped = [], []
        for b in bindings:
            seam = getattr(b.seam_module, b.seam_name, None)
            if seam is None:
                continue
            seam_kwargs = {
                name
                for name, parameter in inspect.signature(seam).parameters.items()
                if parameter.kind is inspect.Parameter.KEYWORD_ONLY
            }
            missing = sorted(
                name for name in seam_kwargs - b.accepted if (b.label, name) not in deliberate
            )
            if missing:
                unreachable.append(f"{b.label} cannot reach {b.seam_name} keywords: {missing}")
            # Accepting a parameter and then not passing it on is the same defect one level
            # in: the door advertises the flag, takes it without complaint, and drops it.
            # `accepted` covers positional-or-keyword too, not only keyword-only — reading
            # `kwonlyargs` alone happened to work for `questions` (all its methods are
            # keyword-only) and silently skipped every method that is not.
            lost = sorted(b.accepted - b.forwarded)
            if lost:
                dropped.append(f"{b.label} accepts these and never passes them on: {lost}")
        self.assertEqual([], unreachable)
        self.assertEqual([], dropped)

    def test_require_decisive_scope_refuses_an_undecided_pairing(self):
        """The opt-in gate: a pairing scope did not make is refused, not reported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first, second = self.same_facet_requests_blocked(target, 2)
            self.deliver_scoped_source(target, "quote-one", {"facet_id": "supplier-quote"})
            self.deliver_scoped_source(target, "quote-two", {"facet_id": "supplier-quote"})
            self.inventory_and_normalize(target)
            one = self.source_id_for(target, "raw/papers/quote-one.html")
            two = self.source_id_for(target, "raw/papers/quote-two.html")
            before = (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8")

            code, payload, _ = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                one,
                "--source-id",
                two,
                "--request-id",
                first,
                "--request-id",
                second,
                "--require-decisive-scope",
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_UNDECIDED", payload["error_code"])
            self.assertEqual("undecided_pairing", payload["details"]["reason"])
            self.assertEqual(
                [first, second],
                [pair["request_id"] for pair in payload["details"]["pairs"]],
            )
            self.assertIn("remediation", payload)
            # Refused before the write: the question is left exactly as it was, so a host
            # can fix its scope keys and retry rather than repair a half-applied reopen.
            self.assertEqual("blocked", self.page_frontmatter(target, "needs-evidence")["status"])
            self.assertEqual(before, (target / "wiki" / "questions" / "needs-evidence.md").read_text(encoding="utf-8"))
            self.assertNotIn("Paired by", (target / "log.md").read_text(encoding="utf-8"))

    def test_require_decisive_scope_allows_a_pairing_scope_determined(self):
        """The gate is about decisiveness, not about scope being present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            heat_request, shade_request = self.two_scoped_requests_blocked(target)
            self.deliver_scoped_source(target, "heat-index", {"facet_id": "heat-index"})
            self.deliver_scoped_source(target, "shade-cover", {"facet_id": "shade-cover"})
            self.inventory_and_normalize(target)

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                self.source_id_for(target, "raw/papers/shade-cover.html"),
                "--source-id",
                self.source_id_for(target, "raw/papers/heat-index.html"),
                "--request-id",
                heat_request,
                "--request-id",
                shade_request,
                "--require-decisive-scope",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])
            self.assertEqual([], payload["warnings"])

    def test_require_decisive_scope_leaves_unscoped_reopens_alone(self):
        """No scope declared means no pairing claimed, so there is nothing to be decisive about.

        The same boundary `fulfill --require-scope` has: a request with no keys states
        nothing for the flag to enforce. Refusing here would break every unscoped reopen
        for a host that turns the flag on globally, and would be enforcing a claim the
        workspace never made.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target, "needs-evidence", query_or_identifier="Heat index readings 2026")
            second = self.add_request(target, "needs-evidence", query_or_identifier="Shade cover survey 2026")
            self.block_on_requests(target, "needs-evidence", [first, second])
            self.deliver_scoped_source(target, "combined-survey", None)
            self.inventory_and_normalize(target)

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                self.source_id_for(target, "raw/papers/combined-survey.html"),
                "--request-id",
                first,
                "--request-id",
                second,
                "--require-decisive-scope",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("open", payload["status"])
            self.assertEqual([], payload["pairs"])

    def test_reopen_does_not_mutate_request_records(self):
        """Fulfilment stays single-writer: reopen computes pairs, it does not record them."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            heat_request, shade_request = self.two_scoped_requests_blocked(target)
            self.deliver_scoped_source(target, "heat-index", {"facet_id": "heat-index"})
            self.deliver_scoped_source(target, "shade-cover", {"facet_id": "shade-cover"})
            self.inventory_and_normalize(target)
            requests_path = target / "sources" / "source-requests.jsonl"
            before = requests_path.read_bytes()

            code, payload, stderr = self.run_resolve(
                target,
                "reopen",
                "--slug",
                "needs-evidence",
                "--agent-id",
                "fetch-agent",
                "--source-id",
                self.source_id_for(target, "raw/papers/heat-index.html"),
                "--source-id",
                self.source_id_for(target, "raw/papers/shade-cover.html"),
                "--request-id",
                heat_request,
                "--request-id",
                shade_request,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual(2, len(payload["pairs"]))
            self.assertEqual(before, requests_path.read_bytes())

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


class QuestionResolveSeamTests(unittest.TestCase):
    """CR-6 T9: the library seam and the CLI are one operation, audit entry included.

    ``tests/test_seam_conformance.py`` holds the two paths to the same *document*.
    It cannot see ``log.md``, which for a resolution is the record that a question
    left the backlog and why. These tests pin the append inside the seam, and pin
    the exit code each refusal family carries — a resolution has two of them, and a
    rewrite that collapses the claim conflict onto exit 2 would be invisible to
    every document-level check in the suite.
    """

    def prepare(self, root: Path, name: str) -> Path:
        scratch = root / name
        scratch.mkdir()
        target = scratch / "resolve-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"},
        ]
        profile_path = scratch / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        with contextlib.redirect_stdout(io.StringIO()):
            CLAIM.main(
                [
                    "--project-root", str(target), "claim",
                    "--slug", "which-benchmarks", "--agent-id", "agent-a", "--format", "json",
                ]
            )
        return target

    def test_the_seam_appends_the_entry_the_cli_appends(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            via_cli = self.prepare(root, "cli")
            via_seam = self.prepare(root, "seam")
            before_cli = (via_cli / "log.md").read_text(encoding="utf-8")
            before_seam = (via_seam / "log.md").read_text(encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = RESOLVE.main(
                    [
                        "--project-root", str(via_cli), "block",
                        "--slug", "which-benchmarks", "--agent-id", "agent-a",
                        "--blocked-reason", "Needs evidence.", "--format", "json",
                    ]
                )
            self.assertEqual(0, int(code or 0), stdout.getvalue())
            report = RESOLVE.run_block(
                via_seam,
                slug="which-benchmarks",
                agent_id="agent-a",
                blocked_reason="Needs evidence.",
            )

            self.assertEqual("blocked", report["status"])
            appended_cli = (via_cli / "log.md").read_text(encoding="utf-8")[len(before_cli):]
            appended_seam = (via_seam / "log.md").read_text(encoding="utf-8")[len(before_seam):]
            self.assertIn("Question blocked", appended_seam)
            self.assertIn("`which-benchmarks` (block)", appended_seam)
            self.assertEqual(appended_cli, appended_seam)

    def test_every_resolution_verb_records_itself(self):
        for verb, call, headline in (
            (
                "defer",
                lambda target: RESOLVE.run_defer(
                    target, slug="which-benchmarks", agent_id="agent-a", reason="Later."
                ),
                "Question deferred",
            ),
            (
                "reject",
                lambda target: RESOLVE.run_reject(
                    target, slug="which-benchmarks", agent_id="agent-a", reason="Out of scope."
                ),
                "Question rejected",
            ),
        ):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as tmpdir:
                target = self.prepare(Path(tmpdir), "seam")
                before = (target / "log.md").read_text(encoding="utf-8")
                call(target)
                appended = (target / "log.md").read_text(encoding="utf-8")[len(before):]
                self.assertIn(headline, appended)
                self.assertIn(f"`which-benchmarks` ({verb})", appended)

    def test_a_claim_conflict_is_exit_three_and_everything_else_is_exit_two(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.prepare(Path(tmpdir), "seam")
            before = (target / "log.md").read_text(encoding="utf-8")
            cases = (
                (
                    lambda: RESOLVE.run_defer(
                        target, slug="which-benchmarks", agent_id="agent-b", reason="Later."
                    ),
                    "CLAIM_HELD",
                    3,
                ),
                (
                    lambda: RESOLVE.run_defer(
                        target, slug="no-such-question", agent_id="agent-a", reason="Later."
                    ),
                    "SLUG_UNKNOWN",
                    2,
                ),
                (
                    lambda: RESOLVE.run_answer(
                        target, slug="which-benchmarks", agent_id="agent-a", answer_page="wiki/nope.md"
                    ),
                    "ANSWER_SOURCE_REQUIRED",
                    2,
                ),
                (
                    lambda: RESOLVE.run_approve(target, slug="which-benchmarks", reviewer="  "),
                    "REVIEWER_INVALID",
                    2,
                ),
            )
            for call, expected_code, expected_exit in cases:
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(RESOLVE.ScriptRefusal) as caught:
                        call()
                    refusal = caught.exception
                    self.assertEqual(expected_code, refusal.error_code)
                    self.assertEqual(expected_exit, refusal.exit_code)
                    envelope = refusal.to_envelope()
                    self.assertEqual(expected_code, envelope["error_code"])
                    self.assertIn("remediation", envelope)
            # A refused resolution writes no audit entry, on this path as on the CLI's.
            self.assertEqual(before, (target / "log.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
