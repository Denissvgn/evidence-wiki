import contextlib
import copy
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
READINESS_SCRIPT_PATH = SCRIPTS / "publication_readiness.py"
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


READINESS = load_script_module("research_publication_readiness", READINESS_SCRIPT_PATH)
INIT = load_script_module("research_publication_readiness_init", INIT_SCRIPT_PATH)


class PublicationReadinessTests(unittest.TestCase):
    def init_workspace(self, root: Path, name: str = "publication-workspace") -> Path:
        target = root / name
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "vendor-product-spec", "question": "What is the vendor product spec?", "priority": "high"}
        ]
        profile_path = root / f"{name}-profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def run_readiness(self, target: Path, *extra: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = READINESS.main(["--project-root", str(target), "--format", "json", *extra])
        self.assertEqual("", stderr.getvalue())
        return int(code or 0), json.loads(stdout.getvalue())

    def test_low_unresolved_license_lint_does_not_block_ship_classification(self):
        reasons: dict[str, list[str]] = {}
        no_ship, attention = READINESS.classify_lint_issues(
            {
                "issues": [
                    {
                        "severity": "LOW",
                        "category": "provenance_license_unresolved",
                        "message": "Automated delivery has explicit unresolved license provenance.",
                    }
                ]
            },
            reasons,
        )

        self.assertFalse(no_ship)
        self.assertFalse(attention)
        self.assertEqual({}, reasons)

    def write_ship_ready_vendor_fixture(self, target: Path) -> None:
        source_id = "web:vendor-official-product-spec"
        raw_path = target / "raw" / "web" / "vendor-product.html"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            "<html><head><title>Official product spec</title></head>"
            "<body>Vendor-controlled product specification.</body></html>\n",
            encoding="utf-8",
        )
        (target / "sources" / "manifest.jsonl").write_text(
            json.dumps(
                {
                    "id": source_id,
                    "kind": "html",
                    "raw_paths": ["raw/web/vendor-product.html"],
                    "status": "normalized",
                    "detected_at": "2026-07-02T12:00:00Z",
                    "provenance": {
                        "origin_url": "https://docs.vendor.example/product/spec",
                        "retrieved_at": "2026-07-02T12:00:00Z",
                        "retrieved_by": "fetch-agent/manual",
                        "license": "Vendor terms",
                        "terms_url": "https://vendor.example/terms",
                        "terms_note": "Official terms reviewed before publication.",
                        "notes": "Official vendor product page captured for publication fixture.",
                        "candidate_id": "cand-vendor-product",
                        "checksum": "sha256:fixture",
                        "checksum_verified": True,
                        "date_not_available": "Official vendor spec page exposes no publication date.",
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        normalized = target / "sources" / "normalized" / "web--vendor-official-product-spec.md"
        normalized.parent.mkdir(parents=True, exist_ok=True)
        normalized.write_text(
            f"""---
type: normalized_source
source_id: {source_id}
source_kind: html
title: Official product spec
provenance:
  origin_url: https://docs.vendor.example/product/spec
  retrieved_at: "2026-07-02T12:00:00Z"
  date_not_available: Official vendor spec page exposes no publication date.
---

# Official product spec

Vendor-controlled product specification.
""",
            encoding="utf-8",
        )
        candidates = target / "sources" / "discovery" / "candidates.jsonl"
        candidates.parent.mkdir(parents=True, exist_ok=True)
        candidates.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-vendor-product",
                    "provider": "search",
                    "url": "https://docs.vendor.example/product/spec",
                    "title": "Official product spec",
                    "source_type": "web_page",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "fetch",
                    "status": "fetched",
                    "selected_for_request_id": "req-vendor-product",
                    "fetched_source_id": source_id,
                    "evidence_path": "vendor_product_spec",
                    "source_policy": "official_vendor",
                    "freshness_policy": "current_product_spec",
                    "identity_policy": "origin_url_matches_candidate",
                    "reasoning": {"risk_flags": []},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        source_note = target / "wiki" / "sources" / "vendor-product-source.md"
        source_note.write_text(
            f"""---
type: source
created: 2026-07-02
updated: 2026-07-02
source_ids:
  - {source_id}
---

# Vendor Product Source

Official vendor source note.
""",
            encoding="utf-8",
        )
        answer = target / "wiki" / "synthesis" / "vendor-product-answer.md"
        answer.write_text(
            f"""---
type: synthesis
created: 2026-07-02
updated: 2026-07-02
source_ids:
  - {source_id}
summary: The vendor product spec is grounded in the official product page.
---

# Vendor Product Spec

The vendor product spec is grounded in the official product page.
""",
            encoding="utf-8",
        )
        question = target / "wiki" / "questions" / "vendor-product-spec.md"
        text = question.read_text(encoding="utf-8")
        text = text.replace("status: open", "status: answered", 1)
        text = text.replace(
            "source_ids: []",
            f"""source_ids:
  - {source_id}
answer_page: ../synthesis/vendor-product-answer.md
coverage_required: true
coverage_manifest: sources/coverage/vendor-product-spec.yml
answered_by: answer-agent
grounding:
  - claim: The product spec is vendor-controlled.
    source_id: {source_id}
    quote: Vendor-controlled product specification.
    location_hint: Official product spec
confidence: high
evidence_strength: corroborated""",
            1,
        )
        question.write_text(text, encoding="utf-8")
        coverage = target / "sources" / "coverage" / "vendor-product-spec.yml"
        coverage.parent.mkdir(parents=True, exist_ok=True)
        coverage.write_text(
            yaml.safe_dump(
                {
                    "schema_version": "1.0",
                    "question_slug": "vendor-product-spec",
                    "created_at": "2026-07-02T12:00:00Z",
                    "updated_at": "2026-07-02T12:00:00Z",
                    "coverage_profile": "vendor-product-spec",
                    "coverage_verdict": "pending",
                    "required_facets": [
                        {
                            "facet_id": "official-spec",
                            "description": "Confirm the product specification from an official vendor page.",
                            "required": True,
                            "evidence_path": "vendor_product_spec",
                            "source_policy": "official_vendor",
                            "freshness_policy": "current_product_spec",
                            "identity_policy": "origin_url_matches_candidate",
                            "min_sources": 1,
                            "accepted_source_ids": [source_id],
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

    def clean_embedded_inputs(self) -> dict:
        return {
            "status": {
                "readiness": {"verdict": "complete", "reasons": [], "verdict_reasons": []},
                "candidates": {
                    "invalid_records": 0,
                    "selection": {"selected_without_request": 0},
                    "rejections": {"missing_reason": 0},
                },
                "coverage": {},
            },
            "lint": {"stats": {"issue_counts": {}}, "issues": []},
            "export": {
                "counts": {"questions": 1},
                "warnings": [],
                "questions": [
                    {
                        "slug": "vendor-product-spec",
                        "status": "answered",
                        "coverage_required": True,
                        "coverage_status": "pass",
                        "coverage_facets": [],
                        "grounding_verification": {
                            "all_verified": True,
                            "grounding": [
                                {
                                    "claim": "The product spec is vendor-controlled.",
                                    "source_id": "web:vendor-official-product-spec",
                                    "result": "verified",
                                }
                            ],
                        },
                        "human_review": {"pending": False},
                        "citations": [],
                    }
                ],
            },
            "citation_verification": {
                "mode": "local",
                "overall_result": "verified",
                "results": [
                    {
                        "source_id": "paper:verified",
                        "result": "verified",
                    }
                ],
            },
        }

    def test_publication_readiness_ships_when_artifacts_are_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)

            code, document = self.run_readiness(target)

        self.assertEqual(0, code)
        self.assertEqual("ship", document["verdict"])
        self.assertEqual("complete", document["workspace_status"]["readiness"]["verdict"])
        self.assertEqual([], document["reasons"]["coverage"])
        self.assertEqual([], document["reasons"]["curation"])
        self.assertEqual([], document["reasons"]["safety"])

    def test_every_readiness_blocker_is_fail_closed_and_machine_actionable(self):
        cases = {
            "questions": "source_quality",
            "coverage": "coverage",
            "citations": "citation_identity",
            "quotes": "grounding",
            "licenses": "curation",
            "manual_review": "safety",
            "currentness": "currentness",
            "contradiction": "contradiction",
            "failed_live_artifact": "citation_identity",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)

            for case, expected_category in cases.items():
                with self.subTest(case=case):
                    inputs = copy.deepcopy(self.clean_embedded_inputs())
                    question = inputs["export"]["questions"][0]
                    if case == "questions":
                        inputs["status"]["readiness"] = {
                            "verdict": "in_progress",
                            "reasons": ["vendor-product-spec remains actionable."],
                            "verdict_reasons": [],
                        }
                    elif case == "coverage":
                        question["coverage_status"] = "blocked"
                    elif case == "citations":
                        inputs["citation_verification"] = {
                            "mode": "local",
                            "overall_result": "no_ship",
                            "results": [{"source_id": "paper:wrong-work", "result": "mismatch"}],
                        }
                    elif case == "quotes":
                        question["grounding_verification"] = {
                            "all_verified": False,
                            "grounding": [
                                {
                                    "claim": "The product spec is vendor-controlled.",
                                    "source_id": "web:vendor-official-product-spec",
                                    "result": "quote_not_at_anchor",
                                }
                            ],
                        }
                    elif case == "licenses":
                        inputs["lint"]["issues"] = [
                            {
                                "severity": "MEDIUM",
                                "category": "curation_missing_terms_license",
                                "message": (
                                    "web:vendor-official-product-spec provenance lacks reviewed license terms."
                                ),
                            }
                        ]
                    elif case == "manual_review":
                        question["human_review"] = {"pending": True}
                    elif case == "currentness":
                        question["coverage_facets"] = [
                            {
                                "facet_id": "official-spec",
                                "policy_results": [
                                    {
                                        "policy": "current_product_spec",
                                        "verdict": "fail",
                                        "reasons": ["Retained source date is outside the currentness window."],
                                    }
                                ],
                            }
                        ]
                    elif case == "contradiction":
                        question["evidence_strength"] = "contested"
                    elif case == "failed_live_artifact":
                        inputs["citation_verification"] = {
                            "mode": "live",
                            "provider": "openalex",
                            "artifact_status": "failed",
                            "overall_result": "no_ship",
                            "network_io_executed": True,
                            "results": [],
                            "warnings": ["Provider response was unavailable."],
                        }

                    document = READINESS.build_readiness_document(target, embedded_inputs=inputs)

                    self.assertNotEqual("ship", document["verdict"])
                    self.assertTrue(document["reasons"][expected_category])
                    blockers = [
                        item
                        for item in document["verdict_reasons"]
                        if item.get("category") == expected_category
                    ]
                    self.assertTrue(blockers)
                    for blocker in blockers:
                        self.assertTrue(blocker.get("artifacts"), blocker)
                        self.assertTrue(blocker.get("policy"), blocker)
                        self.assertTrue(blocker.get("remediation"), blocker)

            clean = READINESS.build_readiness_document(
                target,
                embedded_inputs=self.clean_embedded_inputs(),
            )

        self.assertEqual("ship", clean["verdict"])
        self.assertTrue(all(item.get("policy") for item in clean["verdict_reasons"]))

    def test_publication_readiness_no_ship_when_human_review_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            question = target / "wiki" / "questions" / "vendor-product-spec.md"
            text = question.read_text(encoding="utf-8")
            text = text.replace("status: answered", "status: human_review", 1)
            text = text.replace(
                "answered_by: answer-agent",
                "answered_by: answer-agent\n"
                "human_review_required: true\n"
                "human_review_status: pending\n"
                "human_review_policies:\n"
                "  - manual_review_required",
                1,
            )
            question.write_text(text, encoding="utf-8")

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("no_ship", document["verdict"])
        self.assertTrue(any("pending required human review" in reason for reason in document["reasons"]["safety"]))

    def test_publication_readiness_ships_after_human_review_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            question = target / "wiki" / "questions" / "vendor-product-spec.md"
            text = question.read_text(encoding="utf-8")
            text = text.replace(
                "answered_by: answer-agent",
                "answered_by: answer-agent\n"
                "human_review_required: true\n"
                "human_review_status: approved\n"
                "human_review_approved: true\n"
                "approved_by: reviewer-a\n"
                'approved_at: "2026-07-02T13:00:00Z"',
                1,
            )
            question.write_text(text, encoding="utf-8")

            code, document = self.run_readiness(target)

        self.assertEqual(0, code)
        self.assertEqual("ship", document["verdict"])

    def test_publication_readiness_blocks_on_failed_coverage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            coverage_path = target / "sources" / "coverage" / "vendor-product-spec.yml"
            coverage = yaml.safe_load(coverage_path.read_text(encoding="utf-8"))
            coverage["required_facets"][0]["accepted_source_ids"] = []
            coverage["required_facets"][0]["blocking_request_ids"] = ["req-vendor-product"]
            coverage_path.write_text(yaml.safe_dump(coverage, sort_keys=False), encoding="utf-8")

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("blocked_on_sources", document["verdict"])
        self.assertTrue(any("vendor-product-spec" in reason for reason in document["reasons"]["coverage"]))

    def test_publication_readiness_requires_blocked_request_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            question = target / "wiki" / "questions" / "vendor-product-spec.md"
            question.write_text(
                question.read_text(encoding="utf-8").replace(
                    "status: open",
                    "status: blocked\nblocked_reason: Need an official current product source.",
                    1,
                ),
                encoding="utf-8",
            )

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("attention_required", document["verdict"])
        self.assertEqual("attention_required", document["workspace_status"]["readiness"]["verdict"])
        self.assertTrue(
            any("vendor-product-spec" in reason for reason in document["reasons"]["source_quality"])
        )

    def test_publication_readiness_no_ships_current_legal_source_without_date_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            manifest_path = target / "sources" / "manifest.jsonl"
            record = json.loads(manifest_path.read_text(encoding="utf-8"))
            record["provenance"]["supported_evidence_areas"] = ["current_legal_figure"]
            record["provenance"].pop("date_metadata", None)
            manifest_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("no_ship", document["verdict"])
        self.assertTrue(any("current legal figure" in reason for reason in document["reasons"]["currentness"]))

    def test_publication_readiness_no_ships_withdrawn_standard_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            manifest_path = target / "sources" / "manifest.jsonl"
            record = json.loads(manifest_path.read_text(encoding="utf-8"))
            record["provenance"].update(
                {
                    "origin_url": "https://www.iso.org/standard/77442.html",
                    "source_type": "standards_registry_entry",
                    "supported_evidence_areas": ["standards_registry_reference"],
                    "standards": {
                        "registry_provider": "iso-open-data",
                        "standards_body": "ISO",
                        "designation": "ISO 19131:2022",
                        "title": "Geographic information - Data product specifications",
                        "edition": 2,
                        "status": "withdrawn",
                        "registry_url": "https://www.iso.org/standard/77442.html",
                        "dataset_license": "ODC-BY-1.0",
                    },
                }
            )
            manifest_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
            coverage_path = target / "sources" / "coverage" / "vendor-product-spec.yml"
            coverage = yaml.safe_load(coverage_path.read_text(encoding="utf-8"))
            coverage["required_facets"][0].update(
                {
                    "evidence_path": "standards_registry_reference",
                    "source_policy": "official_standards_registry",
                    "freshness_policy": "current_standard_reference",
                    "identity_policy": "standard_designation_matches_registry",
                }
            )
            coverage_path.write_text(yaml.safe_dump(coverage, sort_keys=False), encoding="utf-8")

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("no_ship", document["verdict"])
        currentness = "\n".join(document["reasons"]["currentness"])
        self.assertIn("current_standard_reference", currentness)
        self.assertIn("standard_status_withdrawn", currentness)

    def test_publication_readiness_no_ships_failed_grounding_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            question = target / "wiki" / "questions" / "vendor-product-spec.md"
            question.write_text(
                question.read_text(encoding="utf-8").replace(
                    "quote: Vendor-controlled product specification.",
                    "quote: Unavailable quote.",
                    1,
                ),
                encoding="utf-8",
            )

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("no_ship", document["verdict"])
        self.assertTrue(any("The product spec is vendor-controlled" in reason for reason in document["reasons"]["grounding"]))
        self.assertTrue(any("web:vendor-official-product-spec" in reason for reason in document["reasons"]["grounding"]))

    def test_publication_readiness_no_ships_when_workspace_leaks_secret(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)
            (target / "runs" / "run-leak").mkdir(parents=True, exist_ok=True)
            (target / "runs" / "run-leak" / "events.jsonl").write_text(
                '{"message":"Authorization: Bearer abcdefghijklmnop"}\n',
                encoding="utf-8",
            )

            code, document = self.run_readiness(target)

        self.assertEqual(1, code)
        self.assertEqual("no_ship", document["verdict"])
        self.assertTrue(any("bearer_token" in reason for reason in document["reasons"]["safety"]))

    def test_publication_readiness_generates_evaluation_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.write_ship_ready_vendor_fixture(target)

            code, document = self.run_readiness(target, "bundle", "--run-id", "run-fixture")

            bundle_dir = target / "runs" / "run-fixture" / "evaluation"
            bundle_files = {path.name for path in bundle_dir.glob("*.json")}

        self.assertEqual(0, code)
        self.assertEqual("bundle", document["action"])
        self.assertEqual("runs/run-fixture/evaluation", document["bundle_dir"])
        for name in (
            "status.json",
            "publication-readiness.json",
            "export.json",
            "lint.json",
            "citation-verification.json",
            "candidate-summary.json",
            "source-request-summary.json",
        ):
            self.assertIn(name, bundle_files)
