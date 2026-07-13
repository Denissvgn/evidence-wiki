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
COVERAGE_SCRIPT_PATH = SCRIPTS / "coverage_manifest.py"


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"missing workspace script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("research_coverage_manifest_init", SCRIPTS / "init_research_workspace.py")


class CoverageManifestCliTests(unittest.TestCase):
    def load_coverage(self):
        return load_script_module("research_coverage_manifest_under_test", COVERAGE_SCRIPT_PATH)

    def init_workspace(self, root: Path, *, domain_pack: str | None = None) -> Path:
        target = root / "coverage-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"},
            {"id": "needs-current-fee", "question": "What is the current fee?", "priority": "high"},
            {"id": "vendor-product-spec", "question": "What does the official product page claim?", "priority": "high"},
        ]
        if domain_pack is not None:
            profile["workspace_init"]["domain_guidance"] = {
                "mode": "domain_pack",
                "rationale": f"The {domain_pack} domain pack matches this coverage-template test workspace.",
            }
            profile["workspace_init"]["domain_pack"] = {"enabled": True, "name": domain_pack}
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = INIT.main(["--profile", str(profile_path)])
        self.assertEqual(0, int(code or 0))
        return target

    def run_coverage(self, module: Any, target: Path, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(["--project-root", str(target), *args])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def run_coverage_json(self, module: Any, target: Path, *args: str) -> tuple[int, dict[str, Any], str]:
        code, stdout, stderr = self.run_coverage(module, target, *args, "--format", "json")
        payload = json.loads(stdout if stdout.strip() else stderr)
        return code, payload, stderr

    def manifest_path(self, target: Path, slug: str = "which-benchmarks") -> Path:
        return target / "sources" / "coverage" / f"{slug}.yml"

    def write_template(self, root: Path) -> Path:
        template = root / "template.yml"
        template.write_text(
            yaml.safe_dump(
                {
                    "coverage_profile": "academic-method-existence",
                    "required_facets": [
                        {
                            "facet_id": "paper-identity",
                            "description": "Confirm the method exists in a real scholarly index.",
                            "required": True,
                            "evidence_path": "academic_method_existence",
                            "source_policy": "academic_indexed",
                            "freshness_policy": "publication_identity",
                            "identity_policy": "citation_id_resolves",
                            "min_sources": 1,
                        }
                    ],
                    "optional_facets": [
                        {
                            "facet_id": "implementation-scope",
                            "description": "Identify implementation scope when available.",
                            "required": False,
                            "evidence_path": "github_implementation",
                            "source_policy": "canonical_repository",
                            "freshness_policy": "release_snapshot",
                            "identity_policy": "repo_ref_resolves",
                            "min_sources": 0,
                        }
                    ],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return template

    def write_github_metadata_template(self, root: Path) -> Path:
        template = root / "github-template.yml"
        template.write_text(
            yaml.safe_dump(
                {
                    "coverage_profile": "github-metadata-review",
                    "required_facets": [
                        {
                            "facet_id": "repository-metadata",
                            "description": "Allow metadata-only GitHub evidence for repository identity review.",
                            "required": True,
                            "evidence_path": "github_implementation",
                            "source_policy": "canonical_repository",
                            "freshness_policy": "release_snapshot",
                            "identity_policy": "repo_ref_resolves",
                            "min_sources": 1,
                            "accepted_artifact_kinds": ["repository_metadata", "release_metadata"],
                        }
                    ],
                    "optional_facets": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return template

    def write_pack_policy_template(self, root: Path) -> Path:
        template = root / "pack-policy-template.yml"
        template.write_text(
            yaml.safe_dump(
                {
                    "coverage_profile": "general-science-recency",
                    "required_facets": [
                        {
                            "facet_id": "study-recency",
                            "description": "Require a domain-review recency decision for the study evidence.",
                            "required": True,
                            "evidence_path": "academic_method_existence",
                            "source_policy": "academic_indexed",
                            "freshness_policy": "pack:general-science/study-recency",
                            "identity_policy": "citation_id_resolves",
                            "min_sources": 1,
                        }
                    ],
                    "optional_facets": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return template

    def write_negative_probe_template(self, root: Path, *, provider: str = "openalex", claim_type: str = "method_or_artifact_existence") -> Path:
        template = root / "negative-probe-template.yml"
        template.write_text(
            yaml.safe_dump(
                {
                    "coverage_profile": "academic-negative-claim-probe",
                    "required_facets": [
                        {
                            "facet_id": "method-or-artifact-existence",
                            "description": "Confirm the named method or artifact exists in arXiv/OpenAlex.",
                            "evidence_path": "academic_method_existence",
                            "source_policy": "openalex_or_arxiv",
                            "freshness_policy": "publication_identity",
                            "identity_policy": "citation_id_resolves",
                            "min_sources": 1,
                            "claim_probe": {
                                "claim_type": claim_type,
                                "claim_text": "TurboQuant is a published scholarly method.",
                                "claim_verdict": "unconfirmed",
                                "limitation": (
                                    "not found in configured providers for this bounded run; "
                                    "not a global nonexistence claim"
                                ),
                                "bounded_provider_results": [
                                    {
                                        "provider": "arxiv",
                                        "query": "TurboQuant",
                                        "max_results": 5,
                                        "result_count": 0,
                                        "exact_match_count": 0,
                                        "network_io_executed": True,
                                    },
                                    {
                                        "provider": provider,
                                        "query": "TurboQuant",
                                        "max_results": 5,
                                        "result_count": 1,
                                        "exact_match_count": 0,
                                        "network_io_executed": True,
                                    },
                                ],
                            },
                        }
                    ],
                    "optional_facets": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return template

    def seed_manifest_source(self, target: Path, source_id: str = "paper:bench-survey") -> None:
        record = {
            "id": source_id,
            "kind": "markdown",
            "raw_paths": ["raw/papers/bench-survey.md"],
            "status": "normalized",
            "detected_at": "2026-06-29T00:00:00Z",
        }
        manifest = target / "sources" / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def seed_unusable_vendor_source(self, target: Path) -> str:
        source_id = "web:vendor-official-error"
        raw_path = target / "raw" / "web" / "vendor-official-error.html"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            "<html><head><title>Official service unavailable</title></head>"
            "<body>Official vendor page is temporarily unavailable.</body></html>",
            encoding="utf-8",
        )
        record = {
            "id": source_id,
            "kind": "html",
            "raw_paths": ["raw/web/vendor-official-error.html"],
            "status": "normalized",
            "detected_at": "2026-07-02T12:00:00Z",
            "provenance": {
                "origin_url": "https://docs.vendor.example/product/spec",
                "retrieved_at": "2026-07-02T12:00:00Z",
                "retrieved_by": "fetch-agent/manual",
                "request_id": "req-vendor-spec",
                "date_not_available": "Official vendor page exposes no publication date.",
                "source_status": "error_page",
                "delivery_failure_code": "official_error_page",
                "delivery_failure_detail": "Official host returned a maintenance page.",
            },
        }
        manifest = target / "sources" / "manifest.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        candidates = target / "sources" / "discovery" / "candidates.jsonl"
        candidates.parent.mkdir(parents=True, exist_ok=True)
        candidates.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-vendor-official",
                    "provider": "search",
                    "url": "https://docs.vendor.example/product/spec",
                    "title": "Official product spec",
                    "source_type": "web_page",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "fetch",
                    "status": "selected",
                    "selected_request_id": "req-vendor-spec",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        coverage = {
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
        }
        path = self.manifest_path(target, "vendor-product-spec")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(coverage, sort_keys=False), encoding="utf-8")
        return source_id

    def seed_source_request(self, target: Path, request_id: str = "req-current-fee") -> None:
        record = {
            "schema_version": "1.0",
            "request_id": request_id,
            "kind": "web",
            "query_or_identifier": "official current fee source",
            "rationale": "Blocks the current-fee facet.",
            "priority": "high",
            "question_slugs": ["which-benchmarks"],
            "status": "open",
            "created_at": "2026-06-29T00:00:00Z",
            "updated_at": "2026-06-29T00:00:00Z",
            "source_id": None,
        }
        requests = target / "sources" / "source-requests.jsonl"
        requests.parent.mkdir(parents=True, exist_ok=True)
        requests.write_text(json.dumps(record) + "\n", encoding="utf-8")

    def test_init_creates_manifest_for_existing_question(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--coverage-profile",
                "general-question",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("init", payload["action"])
            self.assertEqual("which-benchmarks", payload["manifest"]["question_slug"])
            self.assertEqual("general-question", payload["manifest"]["coverage_profile"])
            self.assertEqual("pending", payload["manifest"]["coverage_verdict"])
            path = self.manifest_path(target)
            self.assertTrue(path.is_file())
            saved = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["manifest"], saved)
            self.assertEqual([], saved["required_facets"])
            self.assertEqual([], saved["optional_facets"])

    def test_init_from_template_and_validate_show_round_trip(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_template(root)

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("academic-method-existence", payload["manifest"]["coverage_profile"])
            self.assertEqual("pending", payload["manifest"]["required_facets"][0]["facet_verdict"])
            self.assertEqual([], payload["manifest"]["required_facets"][0]["accepted_source_ids"])
            self.assertEqual([], payload["manifest"]["required_facets"][0]["blocking_request_ids"])

            code, validate_payload, stderr = self.run_coverage_json(coverage, target, "validate", "--slug", "which-benchmarks")
            self.assertEqual(0, code, stderr)
            self.assertTrue(validate_payload["valid"])
            self.assertEqual("which-benchmarks", validate_payload["manifest"]["question_slug"])

            code, show_payload, stderr = self.run_coverage_json(coverage, target, "show", "--slug", "which-benchmarks")
            self.assertEqual(0, code, stderr)
            self.assertEqual(validate_payload["manifest"], show_payload["manifest"])

    def test_init_from_template_accepts_github_artifact_kind_opt_in(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_github_metadata_template(root)

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(0, code, stderr)
            facet = payload["manifest"]["required_facets"][0]
            self.assertEqual(["repository_metadata", "release_metadata"], facet["accepted_artifact_kinds"])

            code, validate_payload, stderr = self.run_coverage_json(coverage, target, "validate", "--slug", "which-benchmarks")
            self.assertEqual(0, code, stderr)
            self.assertTrue(validate_payload["valid"])

    def test_init_from_domain_pack_relative_coverage_template_paths(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), domain_pack="llm-research")
            config = yaml.safe_load((target / "research.yml").read_text(encoding="utf-8"))
            templates = config["domain_pack"]["coverage_templates"]

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                templates["academic-method-feasibility"],
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("academic-method-feasibility", payload["manifest"]["coverage_profile"])
            self.assertEqual("method-identity", payload["manifest"]["required_facets"][0]["facet_id"])
            self.assertEqual("academic_method_existence", payload["manifest"]["required_facets"][0]["evidence_path"])

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "vendor-product-spec",
                "--template",
                templates["vendor-product-spec"],
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("vendor-product-spec", payload["manifest"]["coverage_profile"])
            self.assertEqual("official-spec", payload["manifest"]["required_facets"][0]["facet_id"])
            self.assertEqual("vendor_product_spec", payload["manifest"]["required_facets"][0]["evidence_path"])

    def test_init_from_legal_regulatory_current_figure_template(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), domain_pack="legal-regulatory")
            config = yaml.safe_load((target / "research.yml").read_text(encoding="utf-8"))
            template = config["domain_pack"]["coverage_templates"]["official-current-figure"]

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "needs-current-fee",
                "--template",
                template,
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("official-current-figure", payload["manifest"]["coverage_profile"])
            self.assertEqual("current-official-figure", payload["manifest"]["required_facets"][0]["facet_id"])
            self.assertEqual("legal_current_figure", payload["manifest"]["required_facets"][0]["evidence_path"])
            self.assertEqual("official_primary", payload["manifest"]["required_facets"][0]["source_policy"])

    def test_init_from_standards_compliance_templates(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir), domain_pack="standards-compliance")
            config = yaml.safe_load((target / "research.yml").read_text(encoding="utf-8"))
            templates = config["domain_pack"]["coverage_templates"]

            expected = {
                "official-standard-reference": ("registry-identity", "standards_body_primary"),
                "standards-current-version": ("current-registry-reference", "official_standards_registry"),
                "eu-product-requirement-profile": ("product-requirement-authority", "official_standards_registry"),
                "uk-geospatial-standard-register-entry": ("register-entry", "official_standards_registry"),
            }
            for template_name, (facet_id, source_policy) in expected.items():
                with self.subTest(template_name=template_name):
                    code, payload, stderr = self.run_coverage_json(
                        coverage,
                        target,
                        "init",
                        "--slug",
                        "which-benchmarks",
                        "--template",
                        templates[template_name],
                    )

                    self.assertEqual(0, code, stderr)
                    facet = payload["manifest"]["required_facets"][0]
                    self.assertEqual(template_name, payload["manifest"]["coverage_profile"])
                    self.assertEqual(facet_id, facet["facet_id"])
                    self.assertEqual(source_policy, facet["source_policy"])
                    (target / "sources" / "coverage" / "which-benchmarks.yml").unlink()

    def test_init_from_template_rejects_unknown_policy_identifier(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_template(root)
            document = yaml.safe_load(template.read_text(encoding="utf-8"))
            document["required_facets"][0]["source_policy"] = "whatever_the_web_said"
            template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_POLICY_UNKNOWN", payload["error_code"])
            self.assertFalse(self.manifest_path(target).exists())

    def test_init_from_template_accepts_official_guidance_evidence_path(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_template(root)
            document = yaml.safe_load(template.read_text(encoding="utf-8"))
            document["required_facets"][0]["evidence_path"] = "official_guidance"
            document["required_facets"][0]["source_policy"] = "official_primary"
            document["required_facets"][0]["freshness_policy"] = "no_staleness_check"
            document["required_facets"][0]["identity_policy"] = "official_domain_match"
            template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(0, code, stderr)
            facet = payload["manifest"]["required_facets"][0]
            self.assertEqual("official_guidance", facet["evidence_path"])
            self.assertEqual("official_primary", facet["source_policy"])
            self.assertEqual("official_domain_match", facet["identity_policy"])

    def test_init_from_template_accepts_standards_registry_policy_values(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_template(root)
            document = yaml.safe_load(template.read_text(encoding="utf-8"))
            document["coverage_profile"] = "standards-registry-reference"
            document["required_facets"] = [
                {
                    "facet_id": "registry-identity",
                    "description": "Require an official standards registry entry for the exact designation.",
                    "required": True,
                    "evidence_path": "standards_registry_reference",
                    "source_policy": "official_standards_registry",
                    "freshness_policy": "current_standard_reference",
                    "identity_policy": "standard_designation_matches_registry",
                    "min_sources": 1,
                }
            ]
            document["optional_facets"] = [
                {
                    "facet_id": "product-requirement-linkage",
                    "description": "Carry product requirement linkage when the standard is used for compliance.",
                    "required": False,
                    "evidence_path": "product_requirement_profile",
                    "source_policy": "standards_body_primary",
                    "freshness_policy": "current_product_requirement",
                    "identity_policy": "registry_entry_matches_product_requirement",
                    "min_sources": 0,
                }
            ]
            template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(0, code, stderr)
            required = payload["manifest"]["required_facets"][0]
            optional = payload["manifest"]["optional_facets"][0]
            self.assertEqual("standards_registry_reference", required["evidence_path"])
            self.assertEqual("official_standards_registry", required["source_policy"])
            self.assertEqual("current_standard_reference", required["freshness_policy"])
            self.assertEqual("standard_designation_matches_registry", required["identity_policy"])
            self.assertEqual("product_requirement_profile", optional["evidence_path"])
            self.assertEqual("standards_body_primary", optional["source_policy"])
            self.assertEqual("current_product_requirement", optional["freshness_policy"])
            self.assertEqual("registry_entry_matches_product_requirement", optional["identity_policy"])

    def test_init_from_template_accepts_declared_domain_pack_policy_identifier(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, domain_pack="general-science")
            template = self.write_pack_policy_template(root)

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(0, code, stderr)
            facet = payload["manifest"]["required_facets"][0]
            self.assertEqual("pack:general-science/study-recency", facet["freshness_policy"])

    def test_init_from_template_rejects_undeclared_namespaced_policy_identifier(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root, domain_pack="general-science")
            template = self.write_pack_policy_template(root)
            document = yaml.safe_load(template.read_text(encoding="utf-8"))
            document["required_facets"][0]["freshness_policy"] = "pack:general-science/not-declared"
            template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )

            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_POLICY_UNKNOWN", payload["error_code"])

    def test_init_from_template_preserves_unconfirmed_claim_probe_and_evaluates_blocked(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_negative_probe_template(root)

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )
            self.assertEqual(0, code, stderr)
            facet = payload["manifest"]["required_facets"][0]
            self.assertEqual("academic-negative-claim-probe", payload["manifest"]["coverage_profile"])
            self.assertEqual("method_or_artifact_existence", facet["claim_probe"]["claim_type"])
            self.assertEqual("unconfirmed", facet["claim_probe"]["claim_verdict"])
            self.assertIn("not a global nonexistence claim", facet["claim_probe"]["limitation"])

            code, payload, stderr = self.run_coverage_json(coverage, target, "evaluate", "--slug", "which-benchmarks")
            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", payload["coverage_verdict"])
            self.assertEqual("blocked", payload["manifest"]["required_facets"][0]["facet_verdict"])

    def test_init_from_template_rejects_invalid_claim_probe_shape(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            template = self.write_negative_probe_template(root, provider="semantic-scholar")

            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )
            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_CLAIM_PROBE_INVALID", payload["error_code"])
            self.assertIn("semantic-scholar", payload["message"])

            template = self.write_negative_probe_template(root, claim_type="global_nonexistence")
            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "init",
                "--slug",
                "which-benchmarks",
                "--template",
                str(template),
            )
            self.assertEqual(2, code)
            self.assertEqual("COVERAGE_CLAIM_PROBE_INVALID", payload["error_code"])
            self.assertIn("global_nonexistence", payload["message"])

    def test_set_facet_updates_only_named_facet_and_deduplicates_ids(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.seed_manifest_source(target)
            self.seed_source_request(target)
            template = self.write_template(root)
            self.run_coverage_json(coverage, target, "init", "--slug", "which-benchmarks", "--template", str(template))

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "set-facet",
                "--slug",
                "which-benchmarks",
                "--facet-id",
                "paper-identity",
                "--accepted-source-id",
                "paper:bench-survey",
                "--accepted-source-id",
                "paper:bench-survey",
                "--blocking-request-id",
                "req-current-fee",
            )

            self.assertEqual(0, code, stderr)
            facets = {facet["facet_id"]: facet for facet in payload["manifest"]["required_facets"]}
            self.assertEqual(["paper:bench-survey"], facets["paper-identity"]["accepted_source_ids"])
            self.assertEqual(["req-current-fee"], facets["paper-identity"]["blocking_request_ids"])
            optional = payload["manifest"]["optional_facets"][0]
            self.assertEqual("implementation-scope", optional["facet_id"])
            self.assertEqual([], optional["accepted_source_ids"])
            self.assertEqual([], optional["blocking_request_ids"])

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "set-facet",
                "--slug",
                "which-benchmarks",
                "--facet-id",
                "paper-identity",
                "--clear-blocking-request-ids",
            )

            self.assertEqual(0, code, stderr)
            facets = {facet["facet_id"]: facet for facet in payload["manifest"]["required_facets"]}
            self.assertEqual(["paper:bench-survey"], facets["paper-identity"]["accepted_source_ids"])
            self.assertEqual([], facets["paper-identity"]["blocking_request_ids"])

    def test_set_facet_rejects_unknown_source_and_unlinked_request(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.seed_source_request(target)
            template = self.write_template(root)
            self.run_coverage_json(coverage, target, "init", "--slug", "which-benchmarks", "--template", str(template))

            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "set-facet",
                "--slug",
                "which-benchmarks",
                "--facet-id",
                "paper-identity",
                "--accepted-source-id",
                "paper:missing",
            )
            self.assertEqual(2, code)
            self.assertEqual("SOURCE_UNKNOWN", payload["error_code"])

            requests = target / "sources" / "source-requests.jsonl"
            record = json.loads(requests.read_text(encoding="utf-8"))
            record["question_slugs"] = ["needs-current-fee"]
            requests.write_text(json.dumps(record) + "\n", encoding="utf-8")
            code, payload, _ = self.run_coverage_json(
                coverage,
                target,
                "set-facet",
                "--slug",
                "which-benchmarks",
                "--facet-id",
                "paper-identity",
                "--blocking-request-id",
                "req-current-fee",
            )
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_NOT_LINKED", payload["error_code"])

    def test_evaluate_blocks_failed_required_facet_even_when_optional_passes(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            self.seed_manifest_source(target)
            template = self.write_template(root)
            self.run_coverage_json(coverage, target, "init", "--slug", "which-benchmarks", "--template", str(template))
            self.run_coverage_json(
                coverage,
                target,
                "set-facet",
                "--slug",
                "which-benchmarks",
                "--facet-id",
                "implementation-scope",
                "--accepted-source-id",
                "paper:bench-survey",
            )

            code, payload, stderr = self.run_coverage_json(coverage, target, "evaluate", "--slug", "which-benchmarks")

            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", payload["manifest"]["coverage_verdict"])
            required = payload["manifest"]["required_facets"][0]
            optional = payload["manifest"]["optional_facets"][0]
            self.assertEqual("blocked", required["facet_verdict"])
            self.assertEqual("pass", optional["facet_verdict"])

    def test_evaluate_blocks_required_facet_when_accepted_official_source_is_unusable(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            source_id = self.seed_unusable_vendor_source(target)

            code, payload, stderr = self.run_coverage_json(
                coverage,
                target,
                "evaluate",
                "--slug",
                "vendor-product-spec",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", payload["coverage_verdict"])
            self.assertEqual("blocked", payload["manifest"]["required_facets"][0]["facet_verdict"])
            policy_results = payload["policy_results"]["facets"][0]["policy_results"]
            self.assertEqual(["fail", "fail", "fail"], [result["verdict"] for result in policy_results])
            reason_text = "\n".join(reason for result in policy_results for reason in result["reasons"])
            self.assertIn(source_id, reason_text)
            self.assertIn("delivery_failure_code:official_error_page", reason_text)

    def test_evaluate_blocks_required_current_legal_facet_without_date_metadata(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            source_id = "web:seg-social-current"
            manifest = target / "sources" / "manifest.jsonl"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "html",
                        "raw_paths": ["raw/web/seg-social-current.html"],
                        "status": "normalized",
                        "detected_at": "2026-07-04T12:00:00Z",
                        "provenance": {
                            "origin_url": "https://seg-social.example/cuota-reducida",
                            "retrieved_at": "2026-07-04T12:00:00Z",
                            "publisher": "Seguridad Social",
                            "jurisdiction": "ES",
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            candidates = target / "sources" / "discovery" / "candidates.jsonl"
            candidates.parent.mkdir(parents=True, exist_ok=True)
            candidates.write_text(
                json.dumps(
                    {
                        "candidate_id": "cand-seg-social-current",
                        "url": "https://seg-social.example/cuota-reducida",
                        "source_type": "official_legal",
                        "trust_tier": "official_primary",
                        "official_source": True,
                        "status": "selected",
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            coverage_doc = {
                "schema_version": "1.0",
                "question_slug": "needs-current-fee",
                "created_at": "2026-07-04T12:00:00Z",
                "updated_at": "2026-07-04T12:00:00Z",
                "coverage_profile": "madrid-high-stakes",
                "coverage_verdict": "pending",
                "required_facets": [
                    {
                        "facet_id": "current-fee",
                        "description": "Current official social security fee.",
                        "required": True,
                        "evidence_path": "legal_current_figure",
                        "source_policy": "official_primary",
                        "freshness_policy": "current_legal_figure",
                        "identity_policy": "origin_url_matches_candidate",
                        "min_sources": 1,
                        "accepted_source_ids": [source_id],
                        "blocking_request_ids": [],
                        "facet_verdict": "pending",
                    }
                ],
                "optional_facets": [],
            }
            path = self.manifest_path(target, "needs-current-fee")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(coverage_doc, sort_keys=False), encoding="utf-8")

            code, payload, stderr = self.run_coverage_json(coverage, target, "evaluate", "--slug", "needs-current-fee")

            self.assertEqual(0, code, stderr)
            self.assertEqual("blocked", payload["coverage_verdict"])
            self.assertEqual("blocked", payload["manifest"]["required_facets"][0]["facet_verdict"])
            reason_text = "\n".join(
                reason
                for result in payload["policy_results"]["facets"][0]["policy_results"]
                for reason in result["reasons"]
            )
            self.assertIn("date_metadata", reason_text)

    def test_evaluate_official_guidance_uses_official_policies_not_citation_identity(self):
        coverage = self.load_coverage()
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            source_id = "web:fire-risk-guidance"
            manifest = target / "sources" / "manifest.jsonl"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "html",
                        "raw_paths": ["raw/web/fire-risk-guidance.html"],
                        "status": "normalized",
                        "detected_at": "2026-07-04T12:00:00Z",
                        "provenance": {
                            "origin_url": "https://usfa.fema.gov/fire-risk-guidance",
                            "retrieved_at": "2026-07-04T12:00:00Z",
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            jurisdictions = target / "sources" / "jurisdictions.yml"
            jurisdictions.write_text(
                yaml.safe_dump(
                    {
                        "jurisdiction_profiles": [
                            {
                                "jurisdiction_id": "public-safety-authorities",
                                "name": "Public safety authorities",
                                "official_domains": ["usfa.fema.gov"],
                                "blocked_domains": [],
                            }
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            coverage_doc = {
                "schema_version": "1.0",
                "question_slug": "official-guidance",
                "created_at": "2026-07-04T12:00:00Z",
                "updated_at": "2026-07-04T12:00:00Z",
                "coverage_profile": "generic-official-guidance",
                "coverage_verdict": "pending",
                "required_facets": [
                    {
                        "facet_id": "fire-risk-guidance",
                        "description": "Official response-agency fire-risk guidance.",
                        "required": True,
                        "evidence_path": "official_guidance",
                        "source_policy": "primary_or_official",
                        "freshness_policy": "no_staleness_check",
                        "identity_policy": "official_domain_match",
                        "min_sources": 1,
                        "accepted_source_ids": [source_id],
                        "blocking_request_ids": [],
                        "facet_verdict": "pending",
                    }
                ],
                "optional_facets": [],
            }
            path = self.manifest_path(target, "official-guidance")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(coverage_doc, sort_keys=False), encoding="utf-8")

            code, payload, stderr = self.run_coverage_json(coverage, target, "evaluate", "--slug", "official-guidance")

        self.assertEqual(0, code, stderr)
        self.assertEqual("pass", payload["coverage_verdict"])
        policies = payload["policy_results"]["facets"][0]["policy_results"]
        self.assertEqual(["pass", "pass", "pass"], [policy["verdict"] for policy in policies])
        self.assertNotIn("citation_id_resolves", [policy["policy"] for policy in policies])

    def test_slug_and_coverage_dir_safety_reject_path_like_values(self):
        coverage = self.load_coverage()
        bad_slugs = ("nested/question", "..", "https://example.test/question", r"C:\tmp\question")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            for slug in bad_slugs:
                with self.subTest(slug=slug):
                    code, payload, _ = self.run_coverage_json(coverage, target, "init", "--slug", slug)
                    self.assertEqual(2, code)
                    self.assertEqual("SLUG_INVALID", payload["error_code"])

            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["sources"]["coverage_dir"] = "../coverage"
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            code, payload, _ = self.run_coverage_json(coverage, target, "init", "--slug", "which-benchmarks")
            self.assertEqual(2, code)
            self.assertEqual("CONFIG_INVALID", payload["error_code"])


if __name__ == "__main__":
    unittest.main()
