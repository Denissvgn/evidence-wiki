import os
import py_compile
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from tests._script_loader import load_module_uncached

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

    def test_warm_load_reads_no_script_bytes_and_an_edited_sibling_still_invalidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = Path(tmpdir) / "workspace" / "scripts"
            write_script_asset(script_root, "first")
            first = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertEqual("first", first.origin())

            reads: list[Path] = []
            original_read_bytes = Path.read_bytes

            def counting_read_bytes(self_path: Path) -> bytes:
                reads.append(self_path)
                return original_read_bytes(self_path)

            with mock.patch.object(Path, "read_bytes", counting_read_bytes):
                warm = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertIs(first, warm)
            self.assertEqual([], [path for path in reads if script_root in path.parents], "a warm load must not reread the tree")

            time.sleep(0.01)
            (script_root / "helper.py").write_text("ORIGIN = 'second, longer'\n", encoding="utf-8")
            edited = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertIsNot(first, edited)
            self.assertEqual("second, longer", edited.origin())

    @unittest.skipUnless(os.name == "posix", "ctime cannot be restored from user space only on POSIX")
    def test_same_size_same_mtime_rewrite_is_still_seen_through_ctime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script_root = Path(tmpdir) / "workspace" / "scripts"
            write_script_asset(script_root, "aaaa")
            helper = script_root / "helper.py"
            cached_bytecode = Path(py_compile.compile(str(helper), doraise=True))
            bytecode_before = cached_bytecode.read_bytes()
            before = helper.stat()
            first = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertEqual("aaaa", first.origin())

            time.sleep(0.01)
            helper.write_text("ORIGIN = 'bbbb'\n", encoding="utf-8")
            os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = helper.stat()
            self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))
            edited = cli._load_script(script_root / "target.py", "legacy-name")
            self.assertEqual("bbbb", edited.origin())
            self.assertIsNot(first, edited)
            self.assertEqual(bytecode_before, cached_bytecode.read_bytes())
            self.assertEqual(1, len(cli._SCRIPT_MODULE_CACHE))


class TreeHashMemoTests(unittest.TestCase):
    """The remembered tree hash is bound to the signature it was computed under."""

    def load_loader(self):
        return load_module_uncached("tree_hash_memo_loader_under_test", LOADER_SOURCE)

    def test_hash_is_remembered_and_reused_only_while_the_signature_holds(self):
        loader = self.load_loader()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_script_asset(root, "one")
            first = loader._tree_hash(root)
            with mock.patch.object(loader, "_content_tree_hash", side_effect=AssertionError("must not rehash")):
                self.assertEqual(first, loader._tree_hash(root))
            time.sleep(0.01)
            (root / "helper.py").write_text("ORIGIN = 'two'\n", encoding="utf-8")
            second = loader._tree_hash(root)
            self.assertNotEqual(first, second)
            self.assertEqual(second, loader._content_tree_hash(root))

    def test_hash_is_not_remembered_when_the_tree_moves_during_hashing(self):
        loader = self.load_loader()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_script_asset(root, "one")
            original = loader._content_tree_hash

            def hash_then_edit(script_dir: Path) -> str:
                value = original(script_dir)
                time.sleep(0.01)
                (script_dir / "helper.py").write_text("ORIGIN = 'edited mid-hash'\n", encoding="utf-8")
                return value

            with mock.patch.object(loader, "_content_tree_hash", side_effect=hash_then_edit):
                stale = loader._tree_hash(root)
            self.assertNotIn(root, loader._TREE_HASH_MEMO, "a hash whose tree moved underneath it must not be remembered")
            fresh = loader._tree_hash(root)
            self.assertNotEqual(stale, fresh)
            self.assertEqual(fresh, loader._content_tree_hash(root))
            self.assertEqual(fresh, loader._TREE_HASH_MEMO[root][1])


if __name__ == "__main__":
    unittest.main()
