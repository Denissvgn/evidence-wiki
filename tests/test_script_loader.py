"""One module object per script path, and an explicit way to opt out of it.

Test modules here load workspace scripts by path rather than importing them, and
each one used to do its own loading. Two modules naming the same file differently
therefore held two unrelated module objects, and a patch applied to one had no
effect on work driven through the other -- silently, because the patched copy is
never called and a counter that should have observed one call observes none.

These cases pin the property that closes that off (one module object per resolved
path, whatever the caller calls it), the escape hatch for tests whose subject is
module-level state, and the fact that nothing in this suite loads its own copy any
more.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._script_loader import (
    SCRIPTS,
    load_isolated_module,
    load_module,
    load_module_uncached,
    load_script,
    load_script_uncached,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = SCRIPTS / "source_inventory.py"

FIRST = load_script("script_loader_first_reader", "source_inventory.py")
SECOND = load_script("script_loader_second_reader", "source_inventory.py")


class OneObjectPerPathTests(unittest.TestCase):
    """The defect: two names for one file used to mean two module objects."""

    def test_two_names_for_one_script_are_one_module(self):
        self.assertIs(FIRST, SECOND)

    def test_the_two_entry_point_shapes_agree(self):
        self.assertIs(FIRST, load_module("script_loader_third_reader", INVENTORY_PATH))

    def test_an_unresolved_spelling_of_the_path_is_the_same_script(self):
        detoured = SCRIPTS / ".." / "scripts" / "source_inventory.py"
        self.assertIs(FIRST, load_module("script_loader_detoured_reader", detoured))

    def test_an_attribute_patched_through_one_name_is_visible_through_the_other(self):
        with mock.patch.object(FIRST, "_script_loader_probe", "patched", create=True):
            self.assertEqual("patched", SECOND._script_loader_probe)
        self.assertFalse(hasattr(SECOND, "_script_loader_probe"))

    def test_a_patched_collaborator_counts_the_calls_the_other_name_makes(self):
        """The shape of the false negative that motivated this: an exact count.

        Patch through one handle, call through the other. Two module objects make
        this observe zero calls while every assertion in sight still reads as
        though the collaborator were under control.
        """
        calls: list[str] = []
        original = FIRST.slugify

        def counting(value: str) -> str:
            calls.append(value)
            return original(value)

        with mock.patch.object(FIRST, "slugify", counting):
            SECOND.slugify("Counted Once")

        self.assertEqual(["Counted Once"], calls)

    def test_two_test_modules_share_the_script_they_both_load(self):
        from tests.test_discover_sources import DISCOVER as FROM_DISCOVER_SUITE
        from tests.test_search_discovery import DISCOVER as FROM_SEARCH_SUITE

        self.assertIs(FROM_DISCOVER_SUITE, FROM_SEARCH_SUITE)


class FreshModuleTests(unittest.TestCase):
    """Tests whose subject is module-level state opt out, and get a real opt-out."""

    def test_the_uncached_entry_point_is_not_the_shared_module(self):
        fresh = load_module_uncached("script_loader_fresh_reader", INVENTORY_PATH)
        self.assertIsNot(FIRST, fresh)

    def test_every_uncached_load_is_its_own_module(self):
        first = load_script_uncached("script_loader_fresh_a", "source_inventory.py")
        second = load_script_uncached("script_loader_fresh_b", "source_inventory.py")
        self.assertIsNot(first, second)

    def test_a_fresh_module_does_not_see_the_shared_module_s_patches(self):
        with mock.patch.object(FIRST, "_script_loader_probe", "patched", create=True):
            fresh = load_module_uncached("script_loader_unpatched_reader", INVENTORY_PATH)
            self.assertFalse(hasattr(fresh, "_script_loader_probe"))

    def test_an_uncached_load_does_not_displace_the_shared_module(self):
        load_module_uncached("script_loader_displacing_reader", INVENTORY_PATH)
        self.assertIs(FIRST, load_script("script_loader_after_fresh_reader", "source_inventory.py"))


class PerTestCopyTests(unittest.TestCase):
    """A script under a temporary workspace is a per-test artifact, not a subject."""

    def test_a_script_outside_the_repository_is_never_shared(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "deployed_script.py"
            script.write_text("STATE = []\n", encoding="utf-8")

            first = load_module("script_loader_deployed_first", script)
            first.STATE.append("written by the first test")
            second = load_module("script_loader_deployed_second", script)

            self.assertIsNot(first, second)
            self.assertEqual([], second.STATE)


class LoaderContractTests(unittest.TestCase):
    def test_a_missing_script_names_the_path_it_looked_for(self):
        missing = SCRIPTS / "no_such_workspace_script.py"
        with self.assertRaises(AssertionError) as raised:
            load_module("script_loader_missing_reader", missing)
        self.assertIn(str(missing), str(raised.exception))

    def test_sibling_isolated_loads_share_one_object_under_two_names(self):
        first = load_isolated_module("script_loader_isolated_first", INVENTORY_PATH)
        second = load_isolated_module("script_loader_isolated_second", INVENTORY_PATH)
        self.assertIs(first, second)

    def test_a_sibling_isolated_load_is_its_own_regime(self):
        """Isolation is the thing those callers exercise, so it keeps its own cache."""
        isolated = load_isolated_module("script_loader_isolated_reader", INVENTORY_PATH)
        self.assertIsNot(FIRST, isolated)

    def test_the_packaged_loader_never_claims_its_own_sibling_name(self):
        """Binding it would shadow the plain import every loaded script makes for itself."""
        before = sys.modules.get("_workspace_module_loader")
        load_isolated_module("script_loader_loader_name_probe", INVENTORY_PATH)
        self.assertIs(before, sys.modules.get("_workspace_module_loader"))


class NoPrivateLoadersTests(unittest.TestCase):
    """Nothing under ``tests/`` may go back to loading its own copy of a script.

    Fixtures are excluded: ``tests/fixtures`` holds sample workspaces and stand-in
    executables, which are inputs rather than test code and are free to be anything,
    including deliberately unparseable.
    """

    # Excluded by name rather than by listing what to include, so a directory of test
    # code added later is swept without anyone remembering to add it here.
    NON_TEST_CODE_DIRS = frozenset({"fixtures", "__pycache__"})

    def test_code_under_test_parses(self):
        """The sweep below is only a guarantee if it read every file it should."""
        swept = {path.name for path in self._test_sources()}
        self.assertIn("_script_loader.py", swept)
        self.assertIn("test_script_loader.py", swept)

    def test_only_the_shared_loader_reaches_for_spec_from_file_location(self):
        callers = set()
        for path in self._test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                named = isinstance(node, ast.Attribute) and node.attr == "spec_from_file_location"
                bare = isinstance(node, ast.Name) and node.id == "spec_from_file_location"
                if named or bare:
                    callers.add(path.relative_to(REPO_ROOT).as_posix())
        self.assertEqual({"tests/_script_loader.py"}, callers)

    def _test_sources(self) -> list[Path]:
        root = REPO_ROOT / "tests"
        return sorted(
            path
            for path in root.rglob("*.py")
            if self.NON_TEST_CODE_DIRS.isdisjoint(path.relative_to(root).parts[:-1])
        )
