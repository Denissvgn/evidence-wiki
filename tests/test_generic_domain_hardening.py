import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "generic-domain-battery-workspace"
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
QUESTION_SLUG = "curbside-battery-recycling-pilot-safety"


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


def copy_fixture(root: Path) -> Path:
    target = root / "workspace"
    shutil.copytree(FIXTURE, target)
    return target


class GenericDomainHardeningRegressionTests(unittest.TestCase):
    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_battery_fixture_passes_required_coverage_and_blocks_only_human_review(self):
        coverage_module = load_script_module("gdh_coverage", SCRIPTS / "coverage_manifest.py")
        verify_quotes_module = load_script_module("gdh_verify_quotes", SCRIPTS / "verify_quotes.py")
        export_module = load_script_module("gdh_export", SCRIPTS / "export_answers.py")
        readiness_module = load_script_module("gdh_readiness", SCRIPTS / "publication_readiness.py")
        status_module = load_script_module("gdh_status", SCRIPTS / "workspace_status.py")

        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = copy_fixture(Path(tmpdir))

            coverage_code, coverage_stdout, coverage_stderr = self.run_module(
                coverage_module,
                ["--project-root", str(workspace), "evaluate", "--slug", QUESTION_SLUG, "--format", "json"],
            )
            quote_code, quote_stdout, quote_stderr = self.run_module(
                verify_quotes_module,
                ["--project-root", str(workspace), "--slug", QUESTION_SLUG, "--format", "json"],
            )
            export_code, export_stdout, export_stderr = self.run_module(
                export_module,
                ["--project-root", str(workspace), "--format", "json"],
            )
            status = status_module.build_status_document(workspace)
            readiness = readiness_module.build_readiness_document(workspace, embedded_inputs={"status": status})

        self.assertEqual(0, coverage_code, coverage_stderr)
        coverage_payload = json.loads(coverage_stdout)
        manifest = coverage_payload["manifest"]
        self.assertEqual("pass", manifest["coverage_verdict"])
        required = {facet["facet_id"]: facet for facet in manifest["required_facets"]}
        self.assertEqual(
            {"official-safety-guidance", "fire-risk-standards", "vendor-container-spec"},
            set(required),
        )
        self.assertTrue(all(facet["facet_verdict"] == "pass" for facet in required.values()))
        policy_results_by_facet = {
            facet["facet_id"]: facet["policy_results"]
            for facet in coverage_payload["policy_results"]["facets"]
        }
        official_results = policy_results_by_facet["official-safety-guidance"]
        self.assertEqual(
            {"official_primary": "pass", "current_legal_figure": "pass", "official_domain_match": "pass"},
            {result["policy"]: result["verdict"] for result in official_results},
        )
        vendor_results = policy_results_by_facet["vendor-container-spec"]
        self.assertEqual(
            {"official_vendor": "pass", "current_product_spec": "pass", "origin_url_matches_candidate": "pass"},
            {result["policy"]: result["verdict"] for result in vendor_results},
        )
        optional = manifest["optional_facets"][0]
        self.assertEqual("academic-supplement", optional["facet_id"])
        self.assertEqual(["req-academic-supplement-403"], optional["blocking_request_ids"])

        self.assertEqual(0, quote_code, quote_stderr)
        quote_payload = json.loads(quote_stdout)
        self.assertEqual("verified", quote_payload["overall_result"])
        self.assertEqual(4, quote_payload["counts"]["verified"])

        self.assertEqual(0, export_code, export_stderr)
        export_payload = json.loads(export_stdout)
        self.assertEqual({"count": 1, "source_ids": ["web:official-safety-guidance"]}, export_payload["evidence_usability_overrides"])
        question = export_payload["questions"][0]
        self.assertEqual("pass", question["coverage_status"])
        self.assertTrue(question["grounding_verification"]["all_verified"])
        self.assertEqual({"required": True, "status": "pending", "pending": True}, {key: question["human_review"][key] for key in ("required", "status", "pending")})

        self.assertEqual("pass", status["coverage"]["coverage_verdicts"]["curbside-battery-recycling-pilot-safety"])
        self.assertEqual(1, status["coverage"]["passed"])
        self.assertEqual(0, status["coverage"]["blocked"])
        self.assertEqual(1, status["coverage"]["required_question_counts"]["passed"])
        self.assertEqual(1, status["sources"]["evidence_usability_overrides"]["count"])
        self.assertEqual(["req-academic-supplement-403"], status["sources"]["requests_open_ids"])

        self.assertEqual("no_ship", readiness["verdict"])
        self.assertIn(
            "curbside-battery-recycling-pilot-safety is pending required human review approval.",
            readiness["reasons"]["safety"],
        )
        self.assertEqual("pass", readiness["coverage_summary"]["coverage_verdicts"][QUESTION_SLUG])


if __name__ == "__main__":
    unittest.main()
