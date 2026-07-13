import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "standards-registry-workspace"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


EXPORT = load_script_module("standards_registry_export", SCRIPTS / "export_answers.py")
POLICIES = load_script_module("standards_registry_policies", SCRIPTS / "_evidence_policies.py")
READINESS = load_script_module("standards_registry_publication_readiness", SCRIPTS / "publication_readiness.py")
STATUS = load_script_module("standards_registry_workspace_status", SCRIPTS / "workspace_status.py")


class StandardsRegistryWorkflowTests(unittest.TestCase):
    def copy_fixture(self, root: Path) -> Path:
        target = root / "standards-registry-workspace"
        shutil.copytree(FIXTURE, target)
        return target

    def test_offline_fixture_exports_standards_and_blocks_withdrawn_standard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_fixture(Path(tmpdir))

            status = STATUS.build_status_document(workspace)
            export = EXPORT.build_export(workspace, None)
            inputs = POLICIES.load_policy_inputs(workspace)
            coverage = inputs.coverage_manifests["standards-registry-positive"]
            policy_report = POLICIES.evaluate_coverage_manifest_policies(coverage, inputs)
            readiness = READINESS.build_readiness_document(workspace)

        question = export["questions"][0]
        standards_citations = [
            citation for citation in question["citations"] if isinstance(citation.get("standards"), dict)
        ]
        self.assertGreaterEqual(len(standards_citations), 4)
        citations_by_source = {citation["source_id"]: citation for citation in standards_citations}
        self.assertIn("web:iso-19131", citations_by_source)
        self.assertEqual("ISO 19131:2022", citations_by_source["web:iso-19131"]["standards"].get("designation"))

        failed_policy_results = [
            result
            for facet in policy_report["facets"]
            for result in facet["policy_results"]
            if result["verdict"] == "fail"
        ]
        self.assertTrue(
            any(
                result["policy"] == "current_standard_reference"
                and any("standard_status_withdrawn" in reason for reason in result["reasons"])
                for result in failed_policy_results
            ),
            json.dumps(failed_policy_results, indent=2),
        )
        self.assertEqual("no_ship", readiness["verdict"])
        self.assertTrue(
            any("standard_status_withdrawn" in reason for reason in readiness["reasons"]["currentness"]),
            readiness["reasons"],
        )
        self.assertGreaterEqual(status["coverage"]["manifests_total"], 1)
        self.assertEqual(False, readiness["network_io_executed"])


if __name__ == "__main__":
    unittest.main()
