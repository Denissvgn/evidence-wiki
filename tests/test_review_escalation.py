"""End-to-end acceptance for scoped `human_review` escalation (CR-1).

Each test method is one phase of the change request's acceptance criteria, driven through
the shipped commands against a real initialized workspace rather than through unit seams:

1. Scoped escalation keeps the rest of the workspace moving.
2. A review recorded from a host approval queue satisfies the publication gate.
3. A rejected review returns the question to ordinary open work.
4. A review queue nobody works re-escalates and re-freezes the workspace.
5. The default configuration reproduces the previous workspace-wide behavior.
"""

import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"

PARKED_SLUG = "quote-freshness"
OPEN_SLUG = "supplier-identity"
SOURCE_ID = "raw:supplier-quote-2026"
PACK_POLICY = "pack:market-data/quote-48h"
BASE_POLICY = "manual_review_required"
REVIEW_REF = "approval-queue-42"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("review_escalation_init", "init_research_workspace.py")
CLAIM = load_script_module("review_escalation_claim", "question_claim.py")
RESOLVE = load_script_module("review_escalation_resolve", "question_resolve.py")
STATUS = load_script_module("review_escalation_status", "workspace_status.py")
LINT = load_script_module("review_escalation_lint", "lint.py")
EXPORT = load_script_module("review_escalation_export", "export_answers.py")
READINESS = load_script_module("review_escalation_readiness", "publication_readiness.py")
CONTROLLER = load_script_module("review_escalation_controller", "orchestration_controller.py")
MCP = load_script_module("review_escalation_mcp", "serve_mcp.py")


class ReviewEscalationAcceptanceTests(unittest.TestCase):
    # ----- fixture -------------------------------------------------------------------

    def build_workspace(self, root: Path, *, scoped: bool) -> Path:
        """A workspace with one question parked behind two manual-review policies.

        ``scoped`` selects the CR-1 configuration; without it the workspace keeps the
        previous behavior, which is what phase 5 asserts.
        """
        root.mkdir(parents=True, exist_ok=True)
        target = root / "review-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": PARKED_SLUG, "question": "Is the supplier quote under 48 hours old?", "priority": "high"},
            {"id": OPEN_SLUG, "question": "Does the quoted SKU match the listing?", "priority": "high"},
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, int(INIT.main(["--profile", str(profile_path)]) or 0))

        self.write_config(target, scoped=scoped)
        self.write_evidence(target)
        self.write_coverage_manifest(target, PARKED_SLUG)
        self.write_grounding(target, PARKED_SLUG)
        self.park_question(target, PARKED_SLUG)
        return target

    def park_remaining_question(self, target: Path) -> None:
        """Park the second question too, so pending reviews are the only remaining work."""
        self.write_answer_page(target, OPEN_SLUG)
        self.write_coverage_manifest(target, OPEN_SLUG)
        self.write_grounding(target, OPEN_SLUG)
        self.park_question(target, OPEN_SLUG)

    def write_config(self, target: Path, *, scoped: bool, max_pending_review_hours: int = 168) -> None:
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        # A pack may declare policies this package cannot evaluate; they resolve to
        # manual_review, which is what parks the question.
        config["domain_pack"] = {
            "name": "market-data",
            "policy_vocabularies": {
                "freshness_policy": {
                    PACK_POLICY: "Require a reviewer to confirm the supplier quote is under 48 hours old.",
                }
            },
        }
        if scoped:
            config["review"] = {
                "escalation_scope": "question",
                "max_pending_review_hours": max_pending_review_hours,
            }
        else:
            config.pop("review", None)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def write_evidence(self, target: Path) -> None:
        raw = target / "raw" / "web" / "supplier-quote.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("# Supplier quote\n\nQuoted unit price is 12.40 EUR.\n", encoding="utf-8")
        (target / "sources" / "manifest.jsonl").write_text(
            json.dumps(
                {
                    "id": SOURCE_ID,
                    "kind": "markdown",
                    "raw_paths": ["raw/web/supplier-quote.md"],
                    "status": "normalized",
                    "detected_at": "2026-08-07T00:00:00Z",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        normalized = target / "sources" / "normalized" / "raw--supplier-quote-2026.md"
        normalized.parent.mkdir(parents=True, exist_ok=True)
        normalized.write_text(
            "---\ntype: source\n"
            f"source_id: {SOURCE_ID}\n"
            "title: Supplier quote 2026\n---\n\n"
            "# Supplier quote 2026\n\nQuoted unit price is 12.40 EUR.\n",
            encoding="utf-8",
        )
        self.write_answer_page(target, PARKED_SLUG)

    def write_answer_page(self, target: Path, slug: str) -> None:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / f"{slug}-answer.md").write_text(
            "---\ntype: synthesis\ncreated: 2026-08-07\nupdated: 2026-08-07\n"
            f"source_ids:\n  - {SOURCE_ID}\n"
            "summary: The supplier quote reports a unit price of 12.40 EUR.\n---\n\n"
            "# Supplier quote\n\nThe supplier quote reports a unit price of 12.40 EUR.\n",
            encoding="utf-8",
        )

    def write_coverage_manifest(self, target: Path, slug: str) -> None:
        manifest = target / "sources" / "coverage" / f"{slug}.yml"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0",
                    "question_slug": slug,
                    "created_at": "2026-08-07T00:00:00Z",
                    "updated_at": "2026-08-07T00:00:00Z",
                    "coverage_profile": "supplier-quote",
                    "coverage_verdict": "pending",
                    "required_facets": [
                        {
                            "facet_id": "quote-evidence",
                            "description": "A reviewer confirms the quote is current and correctly identified.",
                            "required": True,
                            "evidence_path": "academic_method_existence",
                            "source_policy": BASE_POLICY,
                            "freshness_policy": PACK_POLICY,
                            "identity_policy": "none",
                            "min_sources": 1,
                            "accepted_source_ids": [SOURCE_ID],
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

    def write_grounding(self, target: Path, slug: str) -> None:
        """Coverage-gated answers carry quote anchors (docs/question-api.md).

        Without them lint reports HIGH `question_grounding_missing`, which would flip the
        verdict for a reason unrelated to review escalation.
        """
        page = target / "wiki" / "questions" / f"{slug}.md"
        page.write_text(
            page.read_text(encoding="utf-8").replace(
                "source_ids: []",
                "source_ids: []\n"
                "grounding:\n"
                "  - claim: The supplier quote reports a unit price of 12.40 EUR.\n"
                f"    source_id: {SOURCE_ID}\n"
                "    quote: Quoted unit price is 12.40 EUR.\n",
                1,
            ),
            encoding="utf-8",
        )

    def park_question(self, target: Path, slug: str) -> None:
        self.run_script(CLAIM, ["claim", "--slug", slug, "--agent-id", "agent-a"], target)
        payload = self.run_script(
            RESOLVE,
            [
                "answer",
                "--slug",
                slug,
                "--agent-id",
                "agent-a",
                "--answer-page",
                f"wiki/synthesis/{slug}-answer.md",
                "--source-id",
                SOURCE_ID,
                "--require-coverage",
                "--require-grounding",
            ],
            target,
        )
        self.assertEqual("human_review", payload["status"])
        frontmatter = self.question_frontmatter(target, slug)
        self.assertEqual([BASE_POLICY, PACK_POLICY], sorted(frontmatter["human_review_policies"]))
        self.assertIn("human_review_requested_at", frontmatter)

    # ----- command helpers -----------------------------------------------------------

    def run_script(self, module, args: list[str], target: Path, *, expect: int = 0) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(["--project-root", str(target), *args, "--format", "json"])
        payload_text = stdout.getvalue().strip() or stderr.getvalue().strip()
        self.assertEqual(expect, int(code or 0), payload_text)
        return json.loads(payload_text) if payload_text else {}

    def status_document(self, target: Path, *extra: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = STATUS.main(["--project-root", str(target), "--format", "json", "--no-cache", *extra])
        return int(code or 0), json.loads(stdout.getvalue())

    def readiness_document(self, target: Path) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = READINESS.main(["--project-root", str(target), "--format", "json"])
        return int(code or 0), json.loads(stdout.getvalue())

    def lint_results(self, target: Path) -> dict:
        return LINT.run_checks(target, LINT.load_config(target))

    def controller(self, target: Path, command: str, *args: str) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CONTROLLER.main(["--project-root", str(target), command, *args, "--format", "json"])
        return int(code or 0), json.loads(stdout.getvalue() or stderr.getvalue())

    def mcp_tool(self, target: Path, name: str) -> dict:
        server = MCP.ResearchWikiMcpServer(target)
        response = server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
        )
        result = response["result"]
        self.assertFalse(result["isError"], result)
        return result["structuredContent"]

    def question_frontmatter(self, target: Path, slug: str) -> dict:
        text = (target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def accept_review(self, target: Path, policy: str, reviewer: str = "ops-principal") -> dict:
        return self.run_script(
            RESOLVE,
            [
                "review",
                "--slug",
                PARKED_SLUG,
                "--policy",
                policy,
                "--verdict",
                "accepted",
                "--reviewed-by",
                reviewer,
                "--review-ref",
                REVIEW_REF,
            ],
            target,
        )

    def age_pending_review(self, target: Path, hours: float) -> None:
        page = target / "wiki" / "questions" / f"{PARKED_SLUG}.md"
        aged = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        page.write_text(
            re.sub(
                r'human_review_requested_at: "[^"]*"',
                f'human_review_requested_at: "{aged}"',
                page.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )

    # ----- phase 1 -------------------------------------------------------------------

    def test_phase_1_scoped_review_keeps_the_rest_of_the_workspace_moving(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=True)

            code, document = self.status_document(target, "--check-complete")
            readiness = document["readiness"]

            # `--check-complete` exit 1 is "still working", not "attention required".
            self.assertEqual(1, code)
            self.assertEqual("in_progress", readiness["verdict"])
            self.assertEqual(1, readiness["questions_awaiting_review"])
            self.assertEqual([PARKED_SLUG], document["questions"]["human_review_slugs"])
            self.assertEqual([OPEN_SLUG], document["questions"]["actionable_slugs"])
            self.assertIn(
                "questions_awaiting_review",
                {reason["code"] for reason in readiness["verdict_reasons"]},
            )

            # The same counter over MCP, which is how a host reads it.
            mcp_payload = self.mcp_tool(target, "workspace_status")
            self.assertEqual("in_progress", mcp_payload["readiness"]["verdict"])
            self.assertEqual(1, mcp_payload["readiness"]["questions_awaiting_review"])

            # `orchestrate next` issues research work for the unparked question.
            self.controller(target, "start", "--orchestration-id", "orch-cr1", "--agent-id", "agent-pm")
            code, order = self.controller(target, "next", "--orchestration-id", "orch-cr1")

            self.assertEqual(0, code)
            self.assertEqual("orchestration_work_order", order["artifact_type"])
            self.assertEqual("research", order["phase"])
            self.assertEqual([OPEN_SLUG], order["scope"]["question_slugs"])

    def test_phase_1b_workspace_with_only_pending_reviews_is_never_complete(self):
        """The `complete` fall-through the backlog names as this CR's most dangerous mistake."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=True)
            self.park_remaining_question(target)

            code, document = self.status_document(target, "--check-complete")
            readiness = document["readiness"]
            reason_codes = {reason["code"] for reason in readiness["verdict_reasons"]}

            self.assertEqual(0, document["questions"]["actionable"])
            self.assertEqual(0, document["questions"]["blocked"])
            self.assertEqual(2, readiness["questions_awaiting_review"])
            # Answers nobody has reviewed are not finished work.
            self.assertNotEqual("complete", readiness["verdict"])
            self.assertNotEqual(0, code)
            self.assertEqual("in_progress", readiness["verdict"])
            self.assertEqual(1, code)
            self.assertIn("questions_awaiting_review_only", reason_codes)

            # The controller cannot ship, and says so with the stable reason and event data
            # instead of issuing a work order scoped to no questions.
            self.controller(target, "start", "--orchestration-id", "orch-awaiting", "--agent-id", "agent-pm")
            code, finished = self.controller(target, "next", "--orchestration-id", "orch-awaiting")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("no_ship", finished["status"])
            self.assertTrue(
                finished["pause_reason"].startswith(CONTROLLER.AWAITING_REVIEW_TERMINAL_REASON),
                finished["pause_reason"],
            )

            events_path = CONTROLLER.events_path(target, "orch-awaiting")
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            finished_event = [event for event in events if event["event_type"] == "session_finished"][-1]
            self.assertEqual(2, finished_event["data"]["questions_awaiting_review"])
            self.assertEqual(
                sorted([OPEN_SLUG, PARKED_SLUG]),
                sorted(finished_event["data"]["question_slugs"]),
            )

    # ----- phase 2 -------------------------------------------------------------------

    def test_phase_2_recorded_external_review_answers_and_clears_the_safety_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=True)

            _, parked_readiness = self.readiness_document(target)
            self.assertTrue(
                any(
                    "pending required human review" in reason
                    for reason in parked_readiness["reasons"]["safety"]
                )
            )

            first = self.accept_review(target, PACK_POLICY)
            self.assertEqual("human_review", first["status"])
            self.assertEqual([BASE_POLICY], first["pending_policies"])

            _, partial_readiness = self.readiness_document(target)
            self.assertTrue(
                any(
                    "pending required human review" in reason
                    for reason in partial_readiness["reasons"]["safety"]
                ),
                "one accepted policy must not clear a second pending policy",
            )

            second = self.accept_review(target, BASE_POLICY, reviewer="reviewer-b")
            self.assertEqual("answered", second["status"])
            self.assertEqual([], second["pending_policies"])

            frontmatter = self.question_frontmatter(target, PARKED_SLUG)
            self.assertEqual("answered", frontmatter["status"])
            self.assertEqual("approved", frontmatter["human_review_status"])
            self.assertTrue(frontmatter["human_review_approved"])
            self.assertEqual("reviewer-b", frontmatter["approved_by"])

            export = self.run_script(EXPORT, [], target)
            record = next(item for item in export["questions"] if item["slug"] == PARKED_SLUG)
            self.assertFalse(record["human_review"]["pending"])
            self.assertEqual(
                [(PACK_POLICY, REVIEW_REF), (BASE_POLICY, REVIEW_REF)],
                [(entry["policy"], entry["review_ref"]) for entry in record["human_reviews"]],
            )

            _, reviewed_readiness = self.readiness_document(target)
            self.assertEqual([], reviewed_readiness["reasons"]["safety"])

    # ----- phase 3 -------------------------------------------------------------------

    def test_phase_3_rejected_review_returns_the_question_to_open_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=True)

            payload = self.run_script(
                RESOLVE,
                [
                    "review",
                    "--slug",
                    PARKED_SLUG,
                    "--policy",
                    PACK_POLICY,
                    "--verdict",
                    "rejected",
                    "--reviewed-by",
                    "ops-principal",
                    "--review-ref",
                    REVIEW_REF,
                    "--note",
                    "The quote is older than 48 hours.",
                ],
                target,
            )

            self.assertEqual("open", payload["status"])
            frontmatter = self.question_frontmatter(target, PARKED_SLUG)
            self.assertEqual("open", frontmatter["status"])
            self.assertEqual("rejected", frontmatter["human_review_status"])
            self.assertNotIn("claimed_by", frontmatter)
            entry = frontmatter["human_reviews"][0]
            self.assertEqual((PACK_POLICY, "rejected", REVIEW_REF), (entry["policy"], entry["verdict"], entry["review_ref"]))
            self.assertEqual("The quote is older than 48 hours.", entry["note"])

            code, document = self.status_document(target, "--check-complete")
            readiness = document["readiness"]

            # Ordinary actionable work: both questions are open again, none awaiting review.
            self.assertEqual(1, code)
            self.assertEqual("in_progress", readiness["verdict"])
            self.assertEqual(0, readiness["questions_awaiting_review"])
            self.assertEqual(
                sorted([OPEN_SLUG, PARKED_SLUG]),
                sorted(document["questions"]["actionable_slugs"]),
            )

            # Publication readiness no longer reports a stale pending review for it.
            _, publication = self.readiness_document(target)
            self.assertEqual([], publication["reasons"]["safety"])

    # ----- phase 4 -------------------------------------------------------------------

    def test_phase_4_stale_review_queue_re_escalates_and_refuses_orchestration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=True)
            self.write_config(target, scoped=True, max_pending_review_hours=24)

            _, fresh = self.status_document(target)
            self.assertEqual("in_progress", fresh["readiness"]["verdict"])
            self.assertEqual(0, fresh["lint"]["issue_counts"].get("HIGH", 0))

            self.age_pending_review(target, hours=200)

            stale = [
                issue
                for issue in self.lint_results(target)["issues"]
                if issue["category"] == "question_human_review_stale"
            ]
            self.assertEqual(1, len(stale))
            self.assertEqual("HIGH", stale[0]["severity"])

            code, document = self.status_document(target, "--check-complete")
            self.assertEqual(4, code)
            self.assertEqual("attention_required", document["readiness"]["verdict"])
            self.assertEqual(1, document["readiness"]["questions_awaiting_review"])

            self.controller(target, "start", "--orchestration-id", "orch-stale", "--agent-id", "agent-pm")
            code, finished = self.controller(target, "next", "--orchestration-id", "orch-stale")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("no_ship", finished["status"])

            # Recording the review closes the loop: the finding and the freeze both clear.
            self.accept_review(target, PACK_POLICY)
            self.accept_review(target, BASE_POLICY, reviewer="reviewer-b")
            code, recovered = self.status_document(target, "--check-complete")

            self.assertEqual(0, recovered["lint"]["issue_counts"].get("HIGH", 0))
            self.assertEqual(1, code)
            self.assertEqual("in_progress", recovered["readiness"]["verdict"])
            self.assertEqual(0, recovered["readiness"]["questions_awaiting_review"])

    # ----- phase 5 -------------------------------------------------------------------

    def test_phase_5_default_configuration_reproduces_workspace_wide_escalation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.build_workspace(Path(tmpdir), scoped=False)

            self.assertNotIn(
                "review",
                yaml.safe_load((target / "research.yml").read_text(encoding="utf-8")),
            )

            code, document = self.status_document(target, "--check-complete")
            readiness = document["readiness"]

            self.assertEqual(4, code)
            self.assertEqual("attention_required", readiness["verdict"])
            self.assertIn(
                f"1 question(s) require human review approval: {PARKED_SLUG}.",
                readiness["reasons"],
            )
            # The counter is reported under both scopes; only the verdict differs.
            self.assertEqual(1, readiness["questions_awaiting_review"])

            # A fresh session terminates no_ship instead of issuing work.
            self.controller(target, "start", "--orchestration-id", "orch-default", "--agent-id", "agent-pm")
            code, finished = self.controller(target, "next", "--orchestration-id", "orch-default")

            self.assertEqual(CONTROLLER.EXIT_INVALID, code)
            self.assertEqual("no_ship", finished["status"])
            self.assertEqual(
                "Workspace health or HIGH validation findings require operator attention.",
                finished["pause_reason"],
            )

            # No stale-review lint finding under workspace scope, at any age.
            self.age_pending_review(target, hours=10_000)
            categories = {issue["category"] for issue in self.lint_results(target)["issues"]}
            self.assertNotIn("question_human_review_stale", categories)

    # ----- cross-phase invariant -----------------------------------------------------

    def test_scope_never_changes_whether_review_happens(self):
        """The gate the CR must not weaken: an unreviewed answer never ships, either way."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scoped = self.build_workspace(root / "scoped", scoped=True)
            default = self.build_workspace(root / "default", scoped=False)

            for target in (scoped, default):
                with self.subTest(workspace=target.parent.name):
                    code, publication = self.readiness_document(target)
                    self.assertEqual(1, code)
                    self.assertEqual("no_ship", publication["verdict"])
                    self.assertTrue(
                        any(
                            "pending required human review" in reason
                            for reason in publication["reasons"]["safety"]
                        )
                    )


if __name__ == "__main__":
    unittest.main()
