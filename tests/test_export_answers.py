import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
EXPORT_SCRIPT_PATH = SCRIPTS / "export_answers.py"
INIT_SCRIPT_PATH = SCRIPTS / "init_research_workspace.py"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXPORT = load_script_module("research_export_answers", EXPORT_SCRIPT_PATH)
INIT = load_script_module("research_export_answers_init", INIT_SCRIPT_PATH)


ANSWER_PAGE = """---
type: synthesis
created: 2026-06-09
updated: 2026-06-09
source_ids:
  - raw:bench-survey-2026
summary: GSM-Hard and ARC-X dominate 2026 reasoning evaluation.
---

# Reasoning Benchmark Landscape

GSM-Hard and ARC-X are the dominant reasoning benchmarks in 2026 papers.
"""

ANSWER_PAGE_NO_SUMMARY = """---
type: synthesis
created: 2026-06-09
updated: 2026-06-09
source_ids: []
---

# Contamination Notes

First paragraph of the
answer body text.

Second paragraph.
"""

MANIFEST_RECORD = {
    "id": "raw:bench-survey-2026",
    "kind": "markdown",
    "raw_paths": ["raw/papers/bench-survey.md"],
    "status": "normalized",
    "detected_at": "2026-06-09T00:00:00Z",
    "provenance": {
        "origin_url": "https://example.org/bench-survey",
        "license": "CC-BY-4.0",
    },
}

NORMALIZED_RECORD = """---
type: normalized_source
source_id: raw:bench-survey-2026
title: Benchmark Survey 2026
---

# Benchmark Survey 2026
"""


class ExportAnswersTests(unittest.TestCase):
    def init_workspace(
        self,
        root: Path,
        name: str = "export-workspace",
        questions: list[dict] | None = None,
        handoff: dict | None = None,
    ) -> Path:
        target = root / name
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = questions or [
            {"id": "benchmarks", "question": "What benchmarks matter?", "priority": "high"},
            {"id": "contamination", "question": "Which datasets are contaminated?"},
            {"id": "open-question", "question": "What remains open?", "priority": "low"},
        ]
        if handoff is not None:
            profile["workspace_init"]["handoff"] = handoff
        profile_path = root / f"{name}-profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def set_question_fields(self, target: Path, slug: str, status: str, extra_yaml: str = "") -> None:
        page = target / "wiki" / "questions" / f"{slug}.md"
        text = page.read_text()
        replacement = f"status: {status}"
        if extra_yaml:
            replacement += f"\n{extra_yaml}"
        page.write_text(text.replace("status: open", replacement, 1))

    def write_coverage_manifest(
        self,
        target: Path,
        slug: str,
        *,
        accepted_source_ids: list[str] | None = None,
        blocking_request_ids: list[str] | None = None,
        claim_probe: bool = False,
    ) -> None:
        path = target / "sources" / "coverage" / f"{slug}.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        claim_probe_block = ""
        if claim_probe:
            claim_probe_block = """    claim_probe:
      claim_type: method_or_artifact_existence
      claim_text: TurboQuant is a published scholarly method.
      claim_verdict: unconfirmed
      limitation: not found in configured providers for this bounded run; not a global nonexistence claim
      bounded_provider_results:
        - provider: arxiv
          query: TurboQuant
          max_results: 5
          result_count: 0
          exact_match_count: 0
          network_io_executed: true
        - provider: openalex
          query: TurboQuant
          max_results: 5
          result_count: 1
          exact_match_count: 0
          network_io_executed: true
"""
        path.write_text(
            f"""schema_version: '1.0'
question_slug: {slug}
created_at: '2026-06-09T00:00:00Z'
updated_at: '2026-06-09T00:00:00Z'
coverage_profile: academic-method-existence
coverage_verdict: pending
required_facets:
  - facet_id: required-identity
    description: Required identity coverage.
    required: true
    evidence_path: academic_method_existence
    source_policy: academic_indexed
    freshness_policy: publication_identity
    identity_policy: citation_id_resolves
    min_sources: 1
    accepted_source_ids: {accepted_source_ids or []}
    blocking_request_ids: {blocking_request_ids or []}
    facet_verdict: pending
{claim_probe_block}\
optional_facets:
  - facet_id: optional-context
    description: Optional context coverage.
    required: false
    evidence_path: academic_method_existence
    source_policy: academic_indexed
    freshness_policy: publication_identity
    identity_policy: citation_id_resolves
    min_sources: 0
    accepted_source_ids: []
    blocking_request_ids: []
    facet_verdict: pending
""",
            encoding="utf-8",
        )

    def write_requests(self, target: Path, records: list[dict]) -> None:
        path = target / "sources" / "source-requests.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def write_candidate(self, target: Path, record: dict) -> None:
        path = target / "sources" / "discovery" / "candidates.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    def write_citation_verification(self, target: Path, report: dict) -> None:
        path = target / "sources" / "citation-verification.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    def seed_answered_workspace(self, root: Path, handoff: dict | None = None) -> Path:
        target = self.init_workspace(root, handoff=handoff)
        (target / "wiki" / "synthesis").mkdir(parents=True, exist_ok=True)
        (target / "wiki" / "synthesis" / "reasoning-benchmarks.md").write_text(ANSWER_PAGE)
        (target / "sources" / "manifest.jsonl").write_text(json.dumps(MANIFEST_RECORD) + "\n")
        (target / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        (target / "sources" / "normalized" / "raw--bench-survey-2026.md").write_text(NORMALIZED_RECORD)
        self.set_question_fields(
            target,
            "benchmarks",
            "answered",
            "answer_page: ../synthesis/reasoning-benchmarks.md",
        )
        self.set_question_fields(
            target,
            "contamination",
            "blocked",
            "blocked_reason: Needs the 2026 contamination audit; not yet delivered.",
        )
        return target

    def run_export(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = EXPORT.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def export_json(self, target: Path, *extra: str) -> tuple[int, dict]:
        code, stdout, _ = self.run_export("--project-root", str(target), *extra)
        return code, json.loads(stdout)

    def question_by_slug(self, document: dict, slug: str) -> dict:
        for record in document["questions"]:
            if record["slug"] == slug:
                return record
        raise AssertionError(f"Question not exported: {slug}")

    def test_answered_question_record_carries_answer_and_citations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir), handoff={"task_id": "chain-task-0042"})

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            self.assertEqual("1.0", document["schema_version"])
            self.assertEqual({"task_id": "chain-task-0042"}, document["project"]["handoff"])
            self.assertEqual(3, document["counts"]["total"])
            self.assertEqual({"blocked": 1, "answered": 1, "open": 1}, dict(document["counts"]["by_status"]))
            self.assertEqual([], document["warnings"])

            answered = self.question_by_slug(document, "benchmarks")
            self.assertEqual("answered", answered["status"])
            self.assertEqual("wiki/synthesis/reasoning-benchmarks.md", answered["answer_page"])
            self.assertEqual(
                "GSM-Hard and ARC-X dominate 2026 reasoning evaluation.",
                answered["answer_summary"],
            )
            self.assertEqual(["raw:bench-survey-2026"], answered["source_ids"])
            self.assertEqual(
                {"required": False, "status": "not_required", "pending": False, "reviewer": None, "approved_at": None, "policies": []},
                answered["human_review"],
            )
            citation = answered["citations"][0]
            self.assertTrue(citation["in_manifest"])
            self.assertEqual(["raw/papers/bench-survey.md"], citation["raw_paths"])
            self.assertEqual("sources/normalized/raw--bench-survey-2026.md", citation["normalized_record"])
            self.assertEqual("Benchmark Survey 2026", citation["title"])
            self.assertEqual("https://example.org/bench-survey", citation["origin_url"])
            self.assertEqual("CC-BY-4.0", citation["license"])

            blocked = self.question_by_slug(document, "contamination")
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual(
                "Needs the 2026 contamination audit; not yet delivered.",
                blocked["blocked_reason"],
            )

    def test_export_surfaces_pending_and_approved_human_review_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "benchmarks.md"
            text = question.read_text(encoding="utf-8")
            text = text.replace("status: answered", "status: human_review", 1)
            text = text.replace(
                "answer_page: ../synthesis/reasoning-benchmarks.md",
                "answer_page: ../synthesis/reasoning-benchmarks.md\n"
                "human_review_required: true\n"
                "human_review_status: pending\n"
                "human_review_policies:\n"
                "  - manual_review_required",
                1,
            )
            question.write_text(text, encoding="utf-8")

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            record = self.question_by_slug(document, "benchmarks")
            self.assertEqual("human_review", record["status"])
            self.assertEqual(
                {
                    "required": True,
                    "status": "pending",
                    "pending": True,
                    "reviewer": None,
                    "approved_at": None,
                    "policies": ["manual_review_required"],
                },
                record["human_review"],
            )

            text = question.read_text(encoding="utf-8")
            text = text.replace("status: human_review", "status: answered", 1)
            text = text.replace("human_review_status: pending", "human_review_status: approved", 1)
            text = text.replace(
                "human_review_policies:\n  - manual_review_required",
                "human_review_policies:\n  - manual_review_required\n"
                "human_review_approved: true\n"
                "approved_by: reviewer-a\n"
                'approved_at: "2026-06-14T12:00:00Z"',
                1,
            )
            question.write_text(text, encoding="utf-8")

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            record = self.question_by_slug(document, "benchmarks")
            self.assertEqual("answered", record["status"])
            self.assertEqual("approved", record["human_review"]["status"])
            self.assertFalse(record["human_review"]["pending"])
            self.assertEqual("reviewer-a", record["human_review"]["reviewer"])

    def test_export_includes_grounding_and_quote_verification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "benchmarks.md"
            question.write_text(
                question.read_text(encoding="utf-8").replace(
                    "answer_page: ../synthesis/reasoning-benchmarks.md",
                    "answer_page: ../synthesis/reasoning-benchmarks.md\n"
                    "grounding:\n"
                    "  - claim: GSM-Hard and ARC-X are reasoning benchmarks.\n"
                    "    source_id: raw:bench-survey-2026\n"
                    "    quote: Benchmark Survey 2026\n"
                    "    location_hint: normalized title",
                    1,
                ),
                encoding="utf-8",
            )

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        answered = self.question_by_slug(document, "benchmarks")
        self.assertEqual(
            [
                {
                    "claim": "GSM-Hard and ARC-X are reasoning benchmarks.",
                    "source_id": "raw:bench-survey-2026",
                    "quote": "Benchmark Survey 2026",
                    "location_hint": "normalized title",
                }
            ],
            answered["grounding"],
        )
        self.assertTrue(answered["grounding_verification"]["all_verified"])
        self.assertEqual("verified", answered["grounding_verification"]["grounding"][0]["result"])

    def test_export_includes_blocked_question_request_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            contamination = target / "wiki" / "questions" / "contamination.md"
            contamination.write_text(
                contamination.read_text(encoding="utf-8").replace(
                    "blocked_reason: Needs the 2026 contamination audit; not yet delivered.",
                    "blocked_reason: Needs the 2026 contamination audit; not yet delivered.\n"
                    "blocking_request_ids:\n"
                    "  - req-contamination-audit\n"
                    "  - req-missing-blocker",
                    1,
                ),
                encoding="utf-8",
            )
            self.write_requests(
                target,
                [
                    {
                        "schema_version": "1.0",
                        "request_id": "req-contamination-audit",
                        "kind": "web",
                        "title": "Official 2026 contamination audit",
                        "query_or_identifier": "official contamination audit 2026",
                        "rationale": "Need the official current audit.",
                        "evidence_area": "benchmark_contamination",
                        "priority": "high",
                        "question_slugs": ["contamination"],
                        "status": "open",
                        "source_id": None,
                    }
                ],
            )

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        blocked = self.question_by_slug(document, "contamination")
        self.assertEqual(["req-contamination-audit", "req-missing-blocker"], blocked["blocking_request_ids"])
        self.assertEqual(["req-missing-blocker"], blocked["missing_blocking_request_ids"])
        self.assertEqual(
            [
                {
                    "request_id": "req-contamination-audit",
                    "title": "Official 2026 contamination audit",
                    "summary": "Official 2026 contamination audit",
                    "status": "open",
                    "question_slugs": ["contamination"],
                    "evidence_area": "benchmark_contamination",
                    "query_or_identifier": "official contamination audit 2026",
                    "rationale": "Need the official current audit.",
                    "source_id": None,
                }
            ],
            blocked["blocking_requests"],
        )

    def test_export_surfaces_coverage_facets_and_linked_source_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "benchmarks.md"
            question.write_text(
                question.read_text(encoding="utf-8").replace(
                    "answer_page: ../synthesis/reasoning-benchmarks.md\n",
                    "answer_page: ../synthesis/reasoning-benchmarks.md\n"
                    "coverage_required: true\n"
                    "coverage_manifest: sources/coverage/benchmarks.yml\n",
                    1,
                ),
                encoding="utf-8",
            )
            self.write_coverage_manifest(
                target,
                "benchmarks",
                blocking_request_ids=["req-current-fee", "req-missing"],
            )
            self.write_requests(
                target,
                [
                    {
                        "schema_version": "1.0",
                        "request_id": "req-current-fee",
                        "kind": "paper",
                        "query_or_identifier": "arXiv:2601.00001",
                        "rationale": "Need current evidence.",
                        "priority": "high",
                        "question_slugs": ["benchmarks"],
                        "status": "open",
                        "source_id": None,
                    }
                ],
            )

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            answered = self.question_by_slug(document, "benchmarks")
            self.assertIs(True, answered["coverage_required"])
            self.assertEqual("sources/coverage/benchmarks.yml", answered["coverage_manifest"])
            self.assertEqual("blocked", answered["coverage_status"])
            self.assertEqual("blocked", answered["coverage_verdict"])
            self.assertEqual(["required-identity"], answered["failed_facets"])
            self.assertEqual(["req-missing"], answered["missing_source_request_ids"])
            self.assertEqual(["req-current-fee"], [record["request_id"] for record in answered["linked_source_requests"]])
            facets = {facet["facet_id"]: facet for facet in answered["coverage_facets"]}
            self.assertEqual("blocked", facets["required-identity"]["facet_verdict"])
            self.assertEqual("not_applicable", facets["optional-context"]["facet_verdict"])

    def test_export_surfaces_unconfirmed_negative_claim_probe_without_citation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(
                Path(tmpdir),
                questions=[
                    {"id": "turboquant-existence", "question": "Does TurboQuant exist?", "priority": "high"}
                ],
            )
            self.set_question_fields(
                target,
                "turboquant-existence",
                "blocked",
                (
                    "blocked_reason: No confirming source found in bounded arXiv/OpenAlex provider searches.\n"
                    "coverage_manifest: sources/coverage/turboquant-existence.yml"
                ),
            )
            self.write_coverage_manifest(
                target,
                "turboquant-existence",
                blocking_request_ids=[],
                claim_probe=True,
            )

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            record = self.question_by_slug(document, "turboquant-existence")
            self.assertEqual("blocked", record["status"])
            self.assertEqual([], record["source_ids"])
            self.assertEqual([], record["citations"])
            self.assertEqual("blocked", record["coverage_status"])
            self.assertEqual("blocked", record["coverage_verdict"])
            self.assertEqual(["required-identity"], record["failed_facets"])
            self.assertEqual(1, len(record["unconfirmed_claims"]))
            claim = record["unconfirmed_claims"][0]
            self.assertEqual("required-identity", claim["facet_id"])
            self.assertEqual("method_or_artifact_existence", claim["claim_type"])
            self.assertEqual("unconfirmed", claim["claim_verdict"])
            self.assertIn("not a global nonexistence claim", claim["limitation"])
            self.assertEqual(["arxiv", "openalex"], [result["provider"] for result in claim["bounded_provider_results"]])
            facets = {facet["facet_id"]: facet for facet in record["coverage_facets"]}
            self.assertEqual("unconfirmed", facets["required-identity"]["claim_probe"]["claim_verdict"])

    def test_export_includes_policy_verification_currentness_and_candidate_trace_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            self.write_candidate(
                target,
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-bench-survey",
                    "provider": "openalex",
                    "url": "https://example.org/bench-survey",
                    "title": "Benchmark Survey 2026",
                    "source_type": "paper",
                    "trust_tier": "primary_non_official",
                    "official_source": None,
                    "recommended_action": "fetch",
                    "status": "fetched",
                    "selected_source_id": "raw:bench-survey-2026",
                    "evidence_path": "academic_method_existence",
                    "source_policy": "academic_indexed",
                    "freshness_policy": "publication_identity",
                    "identity_policy": "citation_id_resolves",
                },
            )
            self.write_citation_verification(
                target,
                {
                    "schema_version": "1.0",
                    "overall_result": "verified",
                    "results": [
                        {
                            "source_id": "raw:bench-survey-2026",
                            "result": "verified",
                            "mode": "local",
                            "provider": None,
                        }
                    ],
                },
            )

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        record = self.question_by_slug(document, "benchmarks")
        self.assertEqual([], record["policy_results"])
        self.assertEqual([], record["currentness"])
        self.assertEqual(
            [
                {
                    "candidate_id": "cand-bench-survey",
                    "status": "fetched",
                    "source_type": "paper",
                    "evidence_path": "academic_method_existence",
                    "trust_tier": "primary_non_official",
                    "recommended_action": "fetch",
                    "selected_for_request_id": None,
                    "selected_source_id": "raw:bench-survey-2026",
                    "url": "https://example.org/bench-survey",
                    "title": "Benchmark Survey 2026",
                }
            ],
            record["candidate_trace"],
        )
        self.assertEqual(
            [
                {
                    "source_id": "raw:bench-survey-2026",
                    "result": "verified",
                    "mode": "local",
                    "provider": None,
                }
            ],
            record["citation_verification"],
        )

    def test_export_surfaces_evidence_usability_override_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            record = dict(MANIFEST_RECORD)
            record["provenance"] = {
                **record["provenance"],
                "evidence_usability_override": {
                    "usable": True,
                    "reviewed_by": "verifier-agent",
                    "reviewed_at": "2026-07-04T12:30:00Z",
                    "reason": "Rich official guidance capture; JavaScript warning is boilerplate.",
                },
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            code, document = self.export_json(target)
            answered = self.question_by_slug(document, "benchmarks")

        self.assertEqual(0, code)
        self.assertEqual(
            {"count": 1, "source_ids": ["raw:bench-survey-2026"]},
            document["evidence_usability_overrides"],
        )
        self.assertEqual(
            {
                "usable": True,
                "reviewed_by": "verifier-agent",
                "reviewed_at": "2026-07-04T12:30:00Z",
                "reason": "Rich official guidance capture; JavaScript warning is boilerplate.",
            },
            answered["citations"][0]["evidence_usability_override"],
        )

    def test_export_refuses_tampered_signed_handoff_when_secret_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                target = self.seed_answered_workspace(
                    Path(tmpdir),
                    handoff={
                        "task_id": "chain-task-0042",
                        "requested_by": "planner-agent",
                        "chain_run_id": "run-2026-06-09-a",
                    },
                )

            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["project"]["handoff"]["chain_run_id"] = "tampered-run"
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_HANDOFF_SECRET": "workspace-secret"}):
                code, stdout, stderr = self.run_export("--project-root", str(target), "--format", "json")

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("HANDOFF_SIGNATURE_INVALID", envelope["error_code"])
            self.assertEqual("invalid", envelope["details"]["handoff_signature_status"])

    def test_citation_surfaces_checksum_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            checksum = "sha256:" + "a" * 64
            record = dict(MANIFEST_RECORD)
            record["provenance"] = {
                **MANIFEST_RECORD["provenance"],
                "checksum": checksum,
                "checksum_verified": True,
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            citation = self.question_by_slug(document, "benchmarks")["citations"][0]
            self.assertEqual(checksum, citation["checksum"])
            self.assertIs(True, citation["checksum_verified"])

    def test_citation_surfaces_academic_publication_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            record = dict(MANIFEST_RECORD)
            record["provenance"] = {
                "origin_url": "https://openalex.org/W260100003",
                "license": None,
                "academic_provider": "openalex",
                "academic_source_type": "metadata_only",
                "venue": "Closed Synthetic Journal",
                "publication_year": 2026,
                "oa_status": "closed",
                "peer_review_status": "publisher_indexed",
                "openalex_work_id": "W260100003",
                "doi": "10.5555/openalex",
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        citation = self.question_by_slug(document, "benchmarks")["citations"][0]
        self.assertIsNone(citation["license"])
        self.assertEqual(
            {
                "provider": "openalex",
                "source_type": "metadata_only",
                "venue": "Closed Synthetic Journal",
                "publication_year": 2026,
                "oa_status": "closed",
                "peer_review_status": "publisher_indexed",
                "openalex_work_id": "W260100003",
                "doi": "10.5555/openalex",
            },
            citation["academic"],
        )

    def test_citation_surfaces_manual_web_evidence_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            record = dict(MANIFEST_RECORD)
            record["provenance"] = {
                "origin_url": "https://seg-social.es/official/current-fee",
                "source_type": "official_web",
                "jurisdiction": "ES",
                "publisher": "Seguridad Social",
                "date_metadata": {"effective_date": "2026-01-01", "valid_for_year": 2026},
                "supported_evidence_areas": ["social_security_contributions", "current_legal_figure"],
                "curation_notes": "Official source captured for current legal figure evaluation.",
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        citation = self.question_by_slug(document, "benchmarks")["citations"][0]
        self.assertEqual("official_web", citation["source_type"])
        self.assertEqual("ES", citation["jurisdiction"])
        self.assertEqual("Seguridad Social", citation["publisher"])
        self.assertEqual({"effective_date": "2026-01-01", "valid_for_year": 2026}, citation["date_metadata"])
        self.assertEqual(
            ["social_security_contributions", "current_legal_figure"],
            citation["supported_evidence_areas"],
        )
        self.assertEqual(
            "Official source captured for current legal figure evaluation.",
            citation["curation_notes"],
        )

    def test_citation_surfaces_standards_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            record = dict(MANIFEST_RECORD)
            record["provenance"] = {
                "origin_url": "https://www.iso.org/standard/77442.html",
                "license": None,
                "source_type": "standards_registry_entry",
                "standards": {
                    "registry_provider": "iso-open-data",
                    "standards_body": "ISO",
                    "designation": "ISO 19131:2022",
                    "status": "published",
                    "registry_url": "https://www.iso.org/standard/77442.html",
                },
            }
            (target / "sources" / "manifest.jsonl").write_text(json.dumps(record) + "\n")

            code, document = self.export_json(target)

        self.assertEqual(0, code)
        citation = self.question_by_slug(document, "benchmarks")["citations"][0]
        self.assertEqual("standards_registry_entry", citation["source_type"])
        self.assertEqual(
            {
                "registry_provider": "iso-open-data",
                "standards_body": "ISO",
                "designation": "ISO 19131:2022",
                "status": "published",
                "registry_url": "https://www.iso.org/standard/77442.html",
            },
            citation["standards"],
        )

    def test_answer_summary_falls_back_to_first_body_paragraph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            (target / "wiki" / "synthesis").mkdir(parents=True, exist_ok=True)
            (target / "wiki" / "synthesis" / "contamination-notes.md").write_text(ANSWER_PAGE_NO_SUMMARY)
            self.set_question_fields(
                target,
                "contamination",
                "answered",
                "answer_page: ../synthesis/contamination-notes.md",
            )

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            answered = self.question_by_slug(document, "contamination")
            self.assertEqual("First paragraph of the answer body text.", answered["answer_summary"])

    def test_missing_answer_page_surfaces_as_warning_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.set_question_fields(
                target,
                "benchmarks",
                "answered",
                "answer_page: ../synthesis/does-not-exist.md",
            )

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            answered = self.question_by_slug(document, "benchmarks")
            self.assertEqual("../synthesis/does-not-exist.md", answered["answer_page"])
            self.assertIsNone(answered["answer_summary"])
            self.assertTrue(any("missing answer page" in warning for warning in document["warnings"]))

    def test_unknown_source_id_yields_citation_with_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.set_question_fields(
                target,
                "benchmarks",
                "open",
                "source_ids:\n- raw:not-in-manifest",
            )
            # set_question_fields replaces status: open with itself plus fields;
            # remove the duplicate empty source_ids list the template rendered.
            page = target / "wiki" / "questions" / "benchmarks.md"
            page.write_text(page.read_text().replace("source_ids: []\n", "", 1))

            code, document = self.export_json(target)

            self.assertEqual(0, code)
            record = self.question_by_slug(document, "benchmarks")
            citation = record["citations"][0]
            self.assertFalse(citation["in_manifest"])
            self.assertEqual([], citation["raw_paths"])
            self.assertTrue(any("not in manifest" in warning for warning in document["warnings"]))

    def test_status_filter_limits_exported_records_but_not_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))

            code, document = self.export_json(target, "--status", "answered")

            self.assertEqual(0, code)
            self.assertEqual(3, document["counts"]["total"])
            self.assertEqual(1, document["counts"]["exported"])
            self.assertEqual(["answered"], document["filters"]["status"])
            self.assertEqual(["benchmarks"], [record["slug"] for record in document["questions"]])

    def test_jsonl_format_emits_envelope_then_question_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))

            code, stdout, _ = self.run_export("--project-root", str(target), "--format", "jsonl")

            self.assertEqual(0, code)
            lines = [json.loads(line) for line in stdout.strip().splitlines()]
            self.assertEqual(4, len(lines))
            self.assertEqual("envelope", lines[0]["record_type"])
            self.assertEqual("1.0", lines[0]["schema_version"])
            self.assertNotIn("questions", lines[0])
            for record in lines[1:]:
                self.assertEqual("question", record["record_type"])

    def test_output_flag_writes_file_and_export_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            output_path = Path(tmpdir) / "export.json"

            code, stdout, _ = self.run_export(
                "--project-root", str(target), "--output", str(output_path)
            )
            self.assertEqual(0, code)
            self.assertEqual("", stdout)
            first = json.loads(output_path.read_text())

            code, second_stdout, _ = self.run_export("--project-root", str(target))
            second = json.loads(second_stdout)

            first.pop("generated_at")
            second.pop("generated_at")
            self.assertEqual(first, second)

    def test_export_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.seed_answered_workspace(Path(tmpdir))
            snapshot = {
                path: path.read_bytes()
                for path in sorted(target.rglob("*"))
                if path.is_file()
            }

            code, _, _ = self.run_export("--project-root", str(target))

            self.assertEqual(0, code)
            after = {
                path: path.read_bytes()
                for path in sorted(target.rglob("*"))
                if path.is_file()
            }
            self.assertEqual(snapshot, after)

    def test_missing_workspace_exits_2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            code, _, stderr = self.run_export("--project-root", str(Path(tmpdir) / "nope"))
            self.assertEqual(2, code)
            self.assertIn("Missing config", stderr)


if __name__ == "__main__":
    unittest.main()
