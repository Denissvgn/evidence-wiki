import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SpdxLicenseIdsSyncTests(unittest.TestCase):
    def test_fetch_and_inventory_spdx_license_ids_stay_in_sync(self):
        fetch_sources = load_script_module("spdx_sync_fetch_sources", SCRIPTS / "fetch_sources.py")
        source_inventory = load_script_module("spdx_sync_source_inventory", SCRIPTS / "source_inventory.py")

        self.assertEqual(source_inventory.SPDX_LICENSE_IDS, fetch_sources.SPDX_LICENSE_IDS)
        self.assertIn("0BSD", fetch_sources.SPDX_LICENSE_IDS)
        self.assertLessEqual(set(fetch_sources.OPENALEX_LICENSE_TO_SPDX.values()), fetch_sources.SPDX_LICENSE_IDS)


if __name__ == "__main__":
    unittest.main()
