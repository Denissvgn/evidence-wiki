import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
METADATA_PATH = REPO_ROOT / "workspace-template" / "workspace-system.yml"


class WorkspaceMetadataTests(unittest.TestCase):
    def load_metadata(self) -> dict:
        document = yaml.safe_load(METADATA_PATH.read_text())
        self.assertIsInstance(document, dict)
        self.assertEqual({"workspace_system"}, set(document))
        metadata = document["workspace_system"]
        self.assertIsInstance(metadata, dict)
        return metadata

    def test_metadata_has_required_readable_fields(self):
        metadata = self.load_metadata()
        required_fields = {
            "starter_version",
            "schema_version",
            "created",
            "compatible_research_yml_contract",
        }

        self.assertTrue(required_fields.issubset(metadata))
        for field in required_fields:
            self.assertIsInstance(metadata[field], str)
            self.assertTrue(metadata[field].strip(), f"{field} must not be empty")

    def test_metadata_values_use_stable_patterns(self):
        metadata = self.load_metadata()

        self.assertRegex(metadata["starter_version"], re.compile(r"^\d+\.\d+\.\d+$"))
        self.assertRegex(metadata["schema_version"], re.compile(r"^\d+\.\d+$"))
        self.assertRegex(metadata["compatible_research_yml_contract"], re.compile(r"^\d+\.\d+$"))
        self.assertRegex(metadata["created"], re.compile(r"^\d{4}-\d{2}-\d{2}$"))

    def test_metadata_stays_domain_neutral(self):
        text = METADATA_PATH.read_text().lower()

        parent_project = "auto" "nomo"
        for forbidden in ("llm-research", parent_project, "pilot-workspaces", "domain_pack"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
