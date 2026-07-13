import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli

LOADER_SOURCE = REPO_ROOT / "workspace-template" / "scripts" / "_workspace_module_loader.py"


def write_script_asset(root: Path, origin: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LOADER_SOURCE, root / "_workspace_module_loader.py")
    (root / "helper.py").write_text(f"ORIGIN = {origin!r}\n", encoding="utf-8")
    (root / "target.py").write_text(
        "import helper\n\n"
        "def origin():\n"
        "    return helper.ORIGIN\n",
        encoding="utf-8",
    )


class ScriptModuleIsolationTests(unittest.TestCase):
    def setUp(self):
        cli._SCRIPT_MODULE_CACHE.clear()
        cli._LOADER_MODULE_CACHE.clear()

    def tearDown(self):
        cli._SCRIPT_MODULE_CACHE.clear()
        cli._LOADER_MODULE_CACHE.clear()

    def test_same_stem_is_scoped_by_asset_tree_and_restores_interpreter_state(self):
        unrelated = ModuleType("helper")
        unrelated.ORIGIN = "unrelated"
        previous_helper = sys.modules.get("helper")
        original_path = list(sys.path)
        sys.modules["helper"] = unrelated
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                first_root = root / "first" / "scripts"
                second_root = root / "second" / "scripts"
                write_script_asset(first_root, "first")
                write_script_asset(second_root, "second")

                first = cli._load_script(first_root / "target.py", "legacy-first-name")
                second = cli._load_script(second_root / "target.py", "legacy-second-name")
                first_again = cli._load_script(first_root / "target.py", "legacy-first-repeat")

                self.assertEqual("first", first.origin())
                self.assertEqual("second", second.origin())
                self.assertIs(first, first_again)
                self.assertIsNot(first, second)
                self.assertIs(unrelated, sys.modules["helper"])
                self.assertEqual(original_path, sys.path)

                loaded_paths = {
                    Path(module.__file__).resolve()
                    for module in sys.modules.values()
                    if isinstance(getattr(module, "__file__", None), str)
                }
                self.assertFalse(any(first_root in path.parents for path in loaded_paths))
                self.assertFalse(any(second_root in path.parents for path in loaded_paths))
        finally:
            if previous_helper is None:
                sys.modules.pop("helper", None)
            else:
                sys.modules["helper"] = previous_helper

    def test_recreated_asset_path_cannot_reuse_deleted_workspace_dependencies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = Path(tmpdir) / "workspace" / "scripts"
            write_script_asset(script_root, "before-delete")
            before = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertEqual("before-delete", before.origin())

            shutil.rmtree(script_root.parent)
            write_script_asset(script_root, "after-recreate")
            after = cli._load_script(script_root / "target.py", "legacy-name")

            self.assertEqual("after-recreate", after.origin())
            self.assertIsNot(before, after)


if __name__ == "__main__":
    unittest.main()
