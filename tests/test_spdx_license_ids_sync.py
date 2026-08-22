import unittest
from pathlib import Path

from tests._script_loader import load_module as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


class SpdxLicenseIdsSyncTests(unittest.TestCase):
    def test_fetch_and_inventory_spdx_license_ids_stay_in_sync(self):
        fetch_sources = load_script_module("spdx_sync_fetch_sources", SCRIPTS / "fetch_sources.py")
        source_inventory = load_script_module("spdx_sync_source_inventory", SCRIPTS / "source_inventory.py")

        self.assertEqual(source_inventory.SPDX_LICENSE_IDS, fetch_sources.SPDX_LICENSE_IDS)
        self.assertIn("0BSD", fetch_sources.SPDX_LICENSE_IDS)
        self.assertLessEqual(set(fetch_sources.OPENALEX_LICENSE_TO_SPDX.values()), fetch_sources.SPDX_LICENSE_IDS)


if __name__ == "__main__":
    unittest.main()
