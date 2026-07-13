import importlib.util
import re
import sys
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "workspace-template" / "docs"
COVERAGE_DOC = DOCS / "coverage-manifest.md"
EVIDENCE_POLICY_DOC = DOCS / "evidence-policies.md"
RESEARCH_YML = REPO_ROOT / "workspace-template" / "research.yml"
COVERAGE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "coverage-manifests"
MADRID_HIGH_STAKES_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "coverage" / "madrid-autonomo-high-stakes.yml"
COVERAGE_SCRIPT = REPO_ROOT / "workspace-template" / "scripts" / "coverage_manifest.py"

REQUIRED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "question_slug",
    "created_at",
    "updated_at",
    "coverage_profile",
    "required_facets",
    "optional_facets",
    "coverage_verdict",
}
REQUIRED_FACET_FIELDS = {
    "facet_id",
    "description",
    "required",
    "evidence_path",
    "source_policy",
    "freshness_policy",
    "identity_policy",
    "min_sources",
    "accepted_source_ids",
    "blocking_request_ids",
    "facet_verdict",
}
OPTIONAL_FACET_FIELDS = {
    "accepted_artifact_kinds",
    "claim_probe",
}
EXPECTED_FIXTURES = {
    "generic-battery-recycling-guidance.coverage.yml",
    "minimal-social-security-fee.coverage.yml",
    "turboquant-existence.coverage.yml",
    "github-implementation.coverage.yml",
    "vendor-product-spec.coverage.yml",
}
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ALLOWED_COVERAGE_VERDICTS = {"pending", "pass", "blocked"}
ALLOWED_FACET_VERDICTS = {"pending", "pass", "blocked", "not_applicable"}
ALLOWED_EVIDENCE_PATHS = {
    "legal_current_figure",
    "academic_method_existence",
    "github_implementation",
    "official_guidance",
    "product_requirement_profile",
    "standards_registry_reference",
    "vendor_product_spec",
}
ALLOWED_SOURCE_POLICIES = {
    "official_primary",
    "primary_or_official",
    "academic_indexed",
    "openalex_or_arxiv",
    "canonical_repository",
    "official_vendor",
    "official_standards_registry",
    "standards_body_primary",
    "domain_pack_allowed",
    "manual_review_required",
}
ALLOWED_FRESHNESS_POLICIES = {
    "current_legal_figure",
    "current_product_spec",
    "current_standard_reference",
    "current_product_requirement",
    "publication_identity",
    "release_snapshot",
    "no_staleness_check",
    "manual_review",
}
ALLOWED_IDENTITY_POLICIES = {
    "citation_id_resolves",
    "origin_url_matches_candidate",
    "repo_ref_resolves",
    "official_domain_match",
    "standard_designation_matches_registry",
    "registry_entry_matches_product_requirement",
    "none",
}
ALLOWED_ARTIFACT_KINDS = {
    "source_archive",
    "repository_metadata",
    "release_metadata",
}


def load_coverage_module():
    spec = importlib.util.spec_from_file_location("coverage_manifest_schema_contract", COVERAGE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {COVERAGE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fixture(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise AssertionError(f"{path.name} must contain a YAML mapping")
    return document


class CoverageManifestSchemaTests(unittest.TestCase):
    def test_fixture_set_is_complete(self):
        self.assertTrue(COVERAGE_FIXTURES.is_dir(), "coverage manifest fixtures directory is missing")
        self.assertEqual(EXPECTED_FIXTURES, {path.name for path in COVERAGE_FIXTURES.glob("*.coverage.yml")})

    def test_fixtures_follow_schema_contract(self):
        for path in sorted(COVERAGE_FIXTURES.glob("*.coverage.yml")):
            with self.subTest(fixture=path.name):
                document = load_fixture(path)
                self.assertEqual("1.0", document.get("schema_version"))
                self.assertEqual(set(), REQUIRED_TOP_LEVEL_FIELDS - set(document))
                expected_slug = path.name.removesuffix(".coverage.yml")
                self.assertEqual(expected_slug, document["question_slug"])
                self.assertRegex(document["created_at"], UTC_TIMESTAMP)
                self.assertRegex(document["updated_at"], UTC_TIMESTAMP)
                self.assertIn(document["coverage_verdict"], ALLOWED_COVERAGE_VERDICTS)
                self.assertIsInstance(document["required_facets"], list)
                self.assertIsInstance(document["optional_facets"], list)
                self.assertGreaterEqual(len(document["required_facets"]), 1)

                for facet in [*document["required_facets"], *document["optional_facets"]]:
                    self.assertIsInstance(facet, dict)
                    self.assertEqual(set(), REQUIRED_FACET_FIELDS - set(facet))
                    self.assertEqual(set(), set(facet) - REQUIRED_FACET_FIELDS - OPTIONAL_FACET_FIELDS)
                    self.assertIsInstance(facet["facet_id"], str)
                    self.assertIsInstance(facet["description"], str)
                    self.assertIsInstance(facet["required"], bool)
                    self.assertIn(facet["evidence_path"], ALLOWED_EVIDENCE_PATHS)
                    self.assertIn(facet["source_policy"], ALLOWED_SOURCE_POLICIES)
                    self.assertIn(facet["freshness_policy"], ALLOWED_FRESHNESS_POLICIES)
                    self.assertIn(facet["identity_policy"], ALLOWED_IDENTITY_POLICIES)
                    self.assertIsInstance(facet["min_sources"], int)
                    self.assertGreaterEqual(facet["min_sources"], 0)
                    self.assertIsInstance(facet["accepted_source_ids"], list)
                    self.assertIsInstance(facet["blocking_request_ids"], list)
                    self.assertIn(facet["facet_verdict"], ALLOWED_FACET_VERDICTS)
                    if "accepted_artifact_kinds" in facet:
                        self.assertIsInstance(facet["accepted_artifact_kinds"], list)
                        self.assertTrue(set(facet["accepted_artifact_kinds"]) <= ALLOWED_ARTIFACT_KINDS)

    def test_madrid_high_stakes_fixture_follows_schema_contract(self):
        coverage = load_coverage_module()
        document = load_fixture(MADRID_HIGH_STAKES_FIXTURE)

        coverage.validate_manifest(document, expected_slug="autonomo-madrid")

        self.assertEqual("blocked", document["coverage_verdict"])
        blocking_request_ids = {
            request_id
            for facet in document["required_facets"]
            for request_id in facet["blocking_request_ids"]
        }
        self.assertEqual(
            {
                "req-20260704-current-autonomo-fee",
                "req-20260704-madrid-irpf-scale",
                "req-20260704-pae-due-circe-individual",
            },
            blocking_request_ids,
        )
        self.assertTrue(
            all(facet["freshness_policy"] == "current_legal_figure" for facet in document["required_facets"])
        )

    def test_acceptance_facets_need_no_special_case_fields(self):
        madrid = load_fixture(COVERAGE_FIXTURES / "minimal-social-security-fee.coverage.yml")
        turboquant = load_fixture(COVERAGE_FIXTURES / "turboquant-existence.coverage.yml")

        madrid_facets = {facet["facet_id"]: facet for facet in madrid["required_facets"]}
        self.assertEqual("legal_current_figure", madrid_facets["current-reduced-fee-amount"]["evidence_path"])
        self.assertEqual("official_primary", madrid_facets["current-reduced-fee-amount"]["source_policy"])
        self.assertEqual("current_legal_figure", madrid_facets["current-reduced-fee-amount"]["freshness_policy"])

        turboquant_facets = {facet["facet_id"]: facet for facet in turboquant["required_facets"]}
        self.assertEqual("academic_method_existence", turboquant_facets["turboquant-existence"]["evidence_path"])
        self.assertEqual("academic_indexed", turboquant_facets["turboquant-existence"]["source_policy"])
        self.assertEqual("citation_id_resolves", turboquant_facets["turboquant-existence"]["identity_policy"])
        probe = turboquant_facets["turboquant-existence"]["claim_probe"]
        self.assertEqual("method_or_artifact_existence", probe["claim_type"])
        self.assertEqual("unconfirmed", probe["claim_verdict"])
        self.assertIn("not a global nonexistence claim", probe["limitation"])
        self.assertNotIn("does not exist", probe["limitation"].lower())
        self.assertEqual(["arxiv", "openalex"], [result["provider"] for result in probe["bounded_provider_results"]])

    def test_docs_and_config_publish_the_contract(self):
        doc = COVERAGE_DOC.read_text(encoding="utf-8")
        config = yaml.safe_load(RESEARCH_YML.read_text(encoding="utf-8"))

        self.assertEqual("sources/coverage", config["sources"]["coverage_dir"])
        self.assertIn("sources/coverage/<slug>.yml", doc)
        self.assertIn("`coverage_manifest`", doc)
        self.assertIn('"schema_version": "1.0"', doc)
        self.assertIn("`sources.coverage_dir`", (DOCS / "research-yml.md").read_text(encoding="utf-8"))
        for field in sorted(REQUIRED_TOP_LEVEL_FIELDS | REQUIRED_FACET_FIELDS):
            with self.subTest(field=field):
                self.assertIn(f"`{field}`", doc)

    def test_evidence_policy_vocabulary_is_published(self):
        coverage = load_coverage_module()
        evidence_doc = EVIDENCE_POLICY_DOC.read_text(encoding="utf-8")
        coverage_doc = COVERAGE_DOC.read_text(encoding="utf-8")

        self.assertEqual(ALLOWED_EVIDENCE_PATHS, coverage.ALLOWED_EVIDENCE_PATHS)
        self.assertEqual(ALLOWED_SOURCE_POLICIES, coverage.ALLOWED_SOURCE_POLICIES)
        self.assertEqual(ALLOWED_FRESHNESS_POLICIES, coverage.ALLOWED_FRESHNESS_POLICIES)
        self.assertEqual(ALLOWED_IDENTITY_POLICIES, coverage.ALLOWED_IDENTITY_POLICIES)
        self.assertEqual(ALLOWED_ARTIFACT_KINDS, coverage.ALLOWED_ARTIFACT_KINDS)
        self.assertIn("[Evidence Policy Vocabulary](evidence-policies.md)", coverage_doc)

        for value in sorted(
            ALLOWED_EVIDENCE_PATHS
            | ALLOWED_ARTIFACT_KINDS
            | ALLOWED_SOURCE_POLICIES
            | ALLOWED_FRESHNESS_POLICIES
            | ALLOWED_IDENTITY_POLICIES
        ):
            with self.subTest(value=value):
                self.assertIn(f"`{value}`", evidence_doc)


if __name__ == "__main__":
    unittest.main()
