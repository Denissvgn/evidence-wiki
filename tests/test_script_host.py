"""Cover the shared packaged-script host that the CLI and the library API share."""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _script_host, cli, resources


class ScriptHostReExportTests(unittest.TestCase):
    """``cli`` kept its loader names after the move, and they are the same objects."""

    def test_cli_reuses_the_shared_loader_and_caches(self):
        self.assertIs(_script_host._load_script, cli._load_script)
        self.assertIs(_script_host._load_workspace_loader, cli._load_workspace_loader)
        self.assertIs(_script_host._SCRIPT_MODULE_CACHE, cli._SCRIPT_MODULE_CACHE)
        self.assertIs(_script_host._LOADER_MODULE_CACHE, cli._LOADER_MODULE_CACHE)


class LoadPackagedScriptTests(unittest.TestCase):
    def test_loads_a_starter_script_by_stem_and_reuses_the_cached_module(self):
        with resources.assets_root() as root:
            module = _script_host.load_packaged_script(root, "_script_errors")
            self.assertTrue(hasattr(module, "error_envelope"))
            self.assertIs(module, _script_host.load_packaged_script(root, "_script_errors"))
            self.assertIs(
                module,
                _script_host._load_script(root / resources.STARTER_DIR / "scripts" / "_script_errors.py", "legacy"),
            )

    def test_missing_stem_is_reported_as_a_fatal_error(self):
        with resources.assets_root() as root:
            with self.assertRaises(SystemExit):
                _script_host.load_packaged_script(root, "definitely_not_a_packaged_script")


class SharedAssetsRootTests(unittest.TestCase):
    def test_returns_the_identical_root_on_every_call(self):
        first = _script_host.shared_assets_root()
        second = _script_host.shared_assets_root()
        self.assertEqual(first, second)
        self.assertIs(first, second)
        self.assertEqual([], resources.missing_required_assets(first))

    def test_does_not_disturb_the_cli_per_command_assets_root(self):
        # The CLI keeps entering ``assets_root()`` itself; the shared root must not
        # pre-empt, invalidate, or otherwise alter what that entry yields.
        shared = _script_host.shared_assets_root()
        with resources.assets_root() as root:
            self.assertEqual([], resources.missing_required_assets(root))
            self.assertTrue((root / resources.STARTER_DIR / "research.yml").is_file())
        self.assertIs(shared, _script_host.shared_assets_root())
        self.assertEqual([], resources.missing_required_assets(shared))


if __name__ == "__main__":
    unittest.main()
