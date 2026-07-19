import contextlib
import importlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


class DomainPackValidationTests(unittest.TestCase):
    def validator(self):
        return importlib.import_module("evidence_wiki.domain_pack_validator")

    def run_validator(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.validator().main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_llm_research_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "llm-research")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertEqual("1.0", payload["schema_version"])
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("llm-research", payload["domain_pack"]["name"])
        self.assertEqual("0.1.0", payload["domain_pack"]["version"])
        self.assertEqual("0.1", payload["domain_pack"]["compatible_research_yml_contract"])
        self.assertEqual(
            {
                "academic-negative-claim-probe": "coverage-templates/academic-negative-claim-probe.yml",
                "academic-method-feasibility": "coverage-templates/academic-method-feasibility.yml",
                "vendor-product-spec": "coverage-templates/vendor-product-spec.yml",
            },
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])
        self.assertEqual(0, payload["smoke_validation"]["summary"]["issue_count"])
        self.assertTrue(all(check["status"] == "pass" for check in payload["checks"]), payload["checks"])

    def test_llm_research_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "llm-research"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_general_science_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "general-science")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("general-science", payload["domain_pack"]["name"])
        self.assertEqual(["arxiv", "openalex"], payload["domain_pack"]["recommended_acquisition"])
        self.assertEqual(["arxiv", "openalex"], payload["domain_pack"]["recommended_discovery"])
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_general_science_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "general-science"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_legal_regulatory_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "legal-regulatory")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("legal-regulatory", payload["domain_pack"]["name"])
        self.assertEqual(
            {"official-current-figure": "coverage-templates/official-current-figure.yml"},
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_legal_regulatory_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "legal-regulatory"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_standards_compliance_pack_validates_by_name(self):
        code, stdout, stderr = self.run_validator("--path", "standards-compliance")
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual("standards-compliance", payload["domain_pack"]["name"])
        self.assertEqual(
            {
                "official-standard-reference": "coverage-templates/official-standard-reference.yml",
                "standards-current-version": "coverage-templates/standards-current-version.yml",
                "eu-product-requirement-profile": "coverage-templates/eu-product-requirement-profile.yml",
                "uk-geospatial-standard-register-entry": "coverage-templates/uk-geospatial-standard-register-entry.yml",
            },
            payload["domain_pack"]["coverage_templates"],
        )
        self.assertEqual([], payload["domain_pack"]["recommended_acquisition"])
        self.assertEqual([], payload["domain_pack"]["recommended_discovery"])
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_standards_compliance_pack_validates_by_path(self):
        pack_path = REPO_ROOT / "domain-packs" / "standards-compliance"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(pack_path.resolve().as_posix(), payload["domain_pack"]["path"])

    def test_recommended_acquisition_rejects_unknown_provider(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "bad-provider-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "bad-provider-pack"
            overlay["domain_pack"]["recommended_acquisition"] = ["arxiv", "unknown-provider"]
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "recommended_acquisition" and "unknown-provider" in check["message"] for check in failures),
            failures,
        )

    def test_coverage_templates_reject_unknown_policy_identifier(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "bad-coverage-template-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "bad-template.yml"
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "bad-template",
                        "required_facets": [
                            {
                                "facet_id": "bad-policy",
                                "description": "This facet uses an invalid policy.",
                                "evidence_path": "academic_method_existence",
                                "source_policy": "whatever_the_web_said",
                                "freshness_policy": "publication_identity",
                                "identity_policy": "citation_id_resolves",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "bad-coverage-template-pack"
            overlay["domain_pack"]["coverage_templates"] = {"bad-template": "coverage-templates/bad-template.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "coverage_templates" and "whatever_the_web_said" in check["message"] for check in failures),
            failures,
        )

    def test_coverage_templates_accept_e39_policy_vocabulary(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "e39-vocabulary-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "e39-vocabulary.yml"
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "e39-vocabulary",
                        "required_facets": [
                            {
                                "facet_id": "indexed-record",
                                "description": "Accept a scholarly record from OpenAlex or arXiv.",
                                "evidence_path": "academic_method_existence",
                                "source_policy": "openalex_or_arxiv",
                                "freshness_policy": "no_staleness_check",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [
                            {
                                "facet_id": "manual-review",
                                "description": "Allow a domain-pack-specific manual review rule.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "domain_pack_allowed",
                                "freshness_policy": "manual_review",
                                "identity_policy": "origin_url_matches_candidate",
                                "min_sources": 0,
                            }
                        ],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "e39-vocabulary-pack"
            overlay["domain_pack"]["coverage_templates"] = {"e39-vocabulary": "coverage-templates/e39-vocabulary.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            {"e39-vocabulary": "coverage-templates/e39-vocabulary.yml"},
            payload["domain_pack"]["coverage_templates"],
        )

    def test_policy_vocabularies_reject_standards_base_policy_redefinition(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "standards-policy-collision-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "standards-policy-collision-pack"
            overlay["domain_pack"]["policy_vocabularies"] = {
                "source_policy": {
                    "official_standards_registry": "Attempt to redefine a base standards policy.",
                },
            }
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(
                check["id"] == "policy_vocabularies"
                and "official_standards_registry" in check["message"]
                and "namespaced id" in check["message"]
                for check in failures
            ),
            failures,
        )

    def test_required_manual_only_policy_requires_human_gated_pack(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "manual-required-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "manual-required.yml"
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "manual-required",
                        "required_facets": [
                            {
                                "facet_id": "needs-review",
                                "description": "This facet cannot ship autonomously.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "manual_review_required",
                                "freshness_policy": "no_staleness_check",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "manual-required-pack"
            overlay["domain_pack"]["coverage_templates"] = {"manual-required": "coverage-templates/manual-required.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        self.assertFalse(payload["domain_pack"]["human_gated"])
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(
                check["id"] == "autonomous_required_facets"
                and "manual_review_required" in check["message"]
                and "human_gated: true" in check["message"]
                for check in failures
            ),
            failures,
        )

    def test_required_manual_only_policy_is_allowed_when_pack_is_human_gated(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "human-gated-pack"
            shutil.copytree(source_pack, pack_path)
            template_path = pack_path / "coverage-templates" / "manual-required.yml"
            template_path.write_text(
                yaml.safe_dump(
                    {
                        "coverage_profile": "manual-required",
                        "required_facets": [
                            {
                                "facet_id": "needs-review",
                                "description": "This facet intentionally requires human review.",
                                "evidence_path": "vendor_product_spec",
                                "source_policy": "manual_review_required",
                                "freshness_policy": "manual_review",
                                "identity_policy": "none",
                                "min_sources": 1,
                            }
                        ],
                        "optional_facets": [],
                    },
                    sort_keys=False,
                )
            )
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "human-gated-pack"
            overlay["domain_pack"]["human_gated"] = True
            overlay["domain_pack"]["coverage_templates"] = {"manual-required": "coverage-templates/manual-required.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["domain_pack"]["human_gated"])
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["autonomous_required_facets"]["status"])

    def test_coverage_templates_reject_unsafe_pack_path(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "unsafe-coverage-template-pack"
            shutil.copytree(source_pack, pack_path)
            overlay_path = pack_path / "research.overlay.yml"
            overlay = yaml.safe_load(overlay_path.read_text())
            overlay["domain_pack"]["name"] = "unsafe-coverage-template-pack"
            overlay["domain_pack"]["coverage_templates"] = {"unsafe-template": "../outside.yml"}
            overlay_path.write_text(yaml.safe_dump(overlay, sort_keys=False))

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(
            any(check["id"] == "coverage_templates" and "must not contain '..'" in check["message"] for check in failures),
            failures,
        )

    def test_pack_tree_rejects_executable_content_before_smoke_execution(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "executable-content-pack"
            shutil.copytree(source_pack, pack_path)
            (pack_path / "install.py").write_text("raise RuntimeError('must never execute')\n", encoding="utf-8")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("install.py", checks["pack_tree_safety"]["files"])
        self.assertEqual("fail", checks["smoke_validation"]["status"])
        self.assertIn("pack tree safety", checks["smoke_validation"]["message"])

    def test_pack_tree_rejects_symlinked_content(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            pack_path = root / "symlink-content-pack"
            shutil.copytree(source_pack, pack_path)
            outside = root / "outside.md"
            outside.write_text("# Outside\n", encoding="utf-8")
            link = pack_path / "linked.md"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlinks are unavailable on this platform: {exc}")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("linked.md", checks["pack_tree_safety"]["files"])

    def test_pack_tree_rejects_portable_path_collision(self):
        source_pack = REPO_ROOT / "domain-packs" / "llm-research"
        with tempfile.TemporaryDirectory() as tmpdir:
            pack_path = Path(tmpdir) / "portable-pack"
            shutil.copytree(source_pack, pack_path)
            (pack_path / "A.md").write_text("upper\n", encoding="utf-8")
            (pack_path / "a.md").write_text("lower\n", encoding="utf-8")
            if len({path.name for path in pack_path.iterdir() if path.name.casefold() == "a.md"}) < 2:
                self.skipTest("filesystem does not preserve case-distinct names")

            code, stdout, stderr = self.run_validator("--path", str(pack_path))
            payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("fail", checks["pack_tree_safety"]["status"])
        self.assertIn("a.md", checks["pack_tree_safety"]["files"])

    def test_corrupted_pack_reports_missing_referenced_file(self):
        pack_path = REPO_ROOT / "tests" / "fixtures" / "domain-packs" / "corrupt-missing-scaffold"

        code, stdout, stderr = self.run_validator("--path", str(pack_path))
        payload = json.loads(stdout)

        self.assertEqual(1, code)
        self.assertEqual("", stderr)
        self.assertFalse(payload["ok"], payload)
        failures = [check for check in payload["checks"] if check["status"] == "fail"]
        self.assertTrue(failures, payload["checks"])
        self.assertTrue(
            any("scaffolds/missing.md" in check.get("files", []) for check in failures),
            failures,
        )
        self.assertTrue(
            any("missing" in check["message"].lower() for check in failures),
            failures,
        )

    def test_missing_pack_json_error_uses_shared_error_envelope(self):
        code, stdout, stderr = self.run_validator("--path", "does-not-exist", "--format", "json")
        envelope = json.loads(stderr)

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertIn("error_code", envelope)
        self.assertIn("Domain pack not found", envelope["message"])
        self.assertIn("remediation", envelope)


if __name__ == "__main__":
    unittest.main()
