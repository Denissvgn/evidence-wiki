"""Cover the embeddable ``Workspace`` handle and its facade skeleton.

The handle is the contract the rest of the library API is built on, so the
cases here are deliberately about its *boundaries* rather than its
convenience: what ``open`` refuses and with which code, that a refusal leaves
the filesystem exactly as it found it, that ``versions()`` reports skew instead
of raising on it, that a closed handle stays closed, and that N handles enter
the process-wide assets root exactly once between them.
"""

import contextlib
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import tempfile

import evidence_wiki
from evidence_wiki import _script_host, errors, resources
from evidence_wiki._facades import (
    CoverageNamespace,
    DiagnosticsNamespace,
    GroundingNamespace,
    Namespace,
    NormalizeNamespace,
    OrchestrateNamespace,
    QuestionsNamespace,
)
from evidence_wiki.workspace import Workspace

WORKSPACE_SYSTEM_TEXT = """\
workspace_system:
  starter_version: "9.9.9"
  schema_version: "0.1"
  created: "2026-05-10"
  compatible_research_yml_contract: "0.1"
"""


def make_workspace(root: Path, *, workspace_system: str | None = WORKSPACE_SYSTEM_TEXT) -> Path:
    """Write the minimum a ``Workspace`` handle recognizes as a workspace."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "research.yml").write_text("project:\n  name: fixture\n", encoding="utf-8")
    if workspace_system is not None:
        (root / "workspace-system.yml").write_text(workspace_system, encoding="utf-8")
    return root


def tree_snapshot(root: Path) -> list[tuple[str, int]]:
    """Return every path below ``root`` with its size, for before/after comparison."""
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size if path.is_file() else -1)
        for path in root.rglob("*")
    )


class WorkspaceOpenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_opens_a_valid_workspace_and_exposes_the_resolved_root(self):
        root = make_workspace(self.tmp / "ws")
        ws = Workspace.open(root)
        self.addCleanup(ws.close)
        self.assertEqual(root.resolve(), ws.root)
        self.assertTrue(ws.root.is_absolute())
        self.assertFalse(ws.closed)

    def test_accepts_a_string_path_and_a_relative_one(self):
        root = make_workspace(self.tmp / "ws")
        ws = Workspace.open(str(root))
        self.addCleanup(ws.close)
        self.assertEqual(root.resolve(), ws.root)

    def test_directory_without_research_yml_is_config_missing(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        with self.assertRaises(errors.ConfigError) as caught:
            Workspace.open(bare)
        self.assertEqual("CONFIG_MISSING", caught.exception.error_code)
        self.assertIn("research.yml", str(caught.exception))

    def test_nonexistent_path_is_workspace_unreadable(self):
        with self.assertRaises(errors.ConfigError) as caught:
            Workspace.open(self.tmp / "definitely" / "not" / "here")
        self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)

    def test_a_file_is_workspace_unreadable_rather_than_a_workspace(self):
        target = self.tmp / "research.yml"
        target.write_text("project: {}\n", encoding="utf-8")
        with self.assertRaises(errors.ConfigError) as caught:
            Workspace.open(target)
        self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)

    def test_every_refusal_is_an_evidence_wiki_error(self):
        # A host that catches only the base class must not be surprised by an
        # untyped OSError escaping from path resolution or a stat.
        for candidate in (self.tmp / "missing", self.tmp):
            with self.subTest(candidate=candidate):
                with self.assertRaises(errors.EvidenceWikiError):
                    Workspace.open(candidate)

    def test_a_failed_open_creates_nothing(self):
        # ``open`` validates; ``init`` creates. A host that points at the wrong
        # path must not find a half-made workspace there afterwards.
        before = tree_snapshot(self.tmp)
        for candidate in (self.tmp / "missing", self.tmp / "also" / "missing", self.tmp):
            with self.assertRaises(errors.ConfigError):
                Workspace.open(candidate)
        self.assertEqual(before, tree_snapshot(self.tmp))
        self.assertFalse((self.tmp / "missing").exists())
        self.assertFalse((self.tmp / "research.yml").exists())

    def test_opening_a_real_deployed_workspace_starter(self):
        # The packaged starter is a workspace by construction; opening it proves
        # the marker check matches what the product actually ships.
        with resources.assets_root() as assets:
            ws = Workspace.open(assets / resources.STARTER_DIR)
            self.addCleanup(ws.close)
            self.assertTrue((ws.root / "research.yml").is_file())


class WorkspaceVersionsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def open_workspace(self, **kwargs) -> Workspace:
        ws = Workspace.open(make_workspace(self.tmp / "ws", **kwargs))
        self.addCleanup(ws.close)
        return ws

    def test_reports_the_package_version_beside_the_workspace_versions(self):
        report = self.open_workspace().versions()
        self.assertEqual(evidence_wiki.__version__, report["package"])
        self.assertEqual(
            {
                "starter_version": "9.9.9",
                "schema_version": "0.1",
                "compatible_research_yml_contract": "0.1",
            },
            report["workspace"],
        )

    def test_absent_workspace_system_reports_none_rather_than_raising(self):
        report = self.open_workspace(workspace_system=None).versions()
        self.assertEqual(evidence_wiki.__version__, report["package"])
        self.assertEqual(
            {"starter_version": None, "schema_version": None, "compatible_research_yml_contract": None},
            report["workspace"],
        )

    def test_corrupt_workspace_system_reports_none_rather_than_raising(self):
        # Every shape that could plausibly reach this file on a damaged or
        # hand-edited workspace has to degrade, not explode: reporting skew is
        # the point, so the file that reveals it must never be able to refuse.
        corrupt = {
            "unparsable yaml": "workspace_system: [unclosed\n",
            "not a mapping": "- just\n- a\n- list\n",
            "empty document": "",
            "workspace_system is a scalar": "workspace_system: nope\n",
            "workspace_system is null": "workspace_system:\n",
            "keys absent": "workspace_system:\n  unrelated: 1\n",
        }
        for index, (label, text) in enumerate(corrupt.items()):
            with self.subTest(label=label):
                case_root = self.tmp / f"ws-corrupt-{index}"
                ws = Workspace.open(make_workspace(case_root, workspace_system=text))
                self.addCleanup(ws.close)
                report = ws.versions()
                self.assertEqual(
                    {"starter_version": None, "schema_version": None, "compatible_research_yml_contract": None},
                    report["workspace"],
                    label,
                )

    def test_unreadable_workspace_system_reports_none_rather_than_raising(self):
        root = make_workspace(self.tmp / "ws")
        # A directory where the metadata file should be: read_text raises OSError.
        (root / "workspace-system.yml").unlink()
        (root / "workspace-system.yml").mkdir()
        ws = Workspace.open(root)
        self.addCleanup(ws.close)
        self.assertIsNone(ws.versions()["workspace"]["starter_version"])

    def test_open_does_not_gate_on_version_skew(self):
        # AC-3: the API must serve every workspace the CLI serves. A wildly
        # incompatible starter_version is reported, never refused.
        ws = self.open_workspace(
            workspace_system='workspace_system:\n  starter_version: "999.0.0"\n  schema_version: "42"\n'
        )
        self.assertEqual("999.0.0", ws.versions()["workspace"]["starter_version"])


class WorkspaceLifetimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = make_workspace(self.tmp / "ws")

    def test_close_then_use_raises_a_typed_error(self):
        ws = Workspace.open(self.root)
        ws.close()
        self.assertTrue(ws.closed)
        for label, call in (
            ("versions", ws.versions),
            ("_script", lambda: ws._script("_script_errors")),
            ("namespace script", lambda: ws.coverage._script("_script_errors")),
            ("__enter__", ws.__enter__),
        ):
            with self.subTest(label=label):
                with self.assertRaises(errors.ConfigError) as caught:
                    call()
                self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)

    def test_close_is_idempotent_and_leaves_identity_readable(self):
        ws = Workspace.open(self.root)
        ws.close()
        ws.close()
        self.assertTrue(ws.closed)
        self.assertEqual(self.root.resolve(), ws.root)
        self.assertIn("closed", repr(ws))

    def test_context_manager_yields_the_handle_and_closes_on_exit(self):
        with Workspace.open(self.root) as ws:
            self.assertIsInstance(ws, Workspace)
            self.assertFalse(ws.closed)
            self.assertEqual(self.root.resolve(), ws.root)
        self.assertTrue(ws.closed)

    def test_context_manager_closes_even_when_the_body_raises(self):
        sentinel = RuntimeError("body failed")
        with contextlib.suppress(RuntimeError):
            with Workspace.open(self.root) as ws:
                raise sentinel
        self.assertTrue(ws.closed)

    def test_exit_does_not_suppress_the_body_exception(self):
        with self.assertRaises(ValueError):
            with Workspace.open(self.root):
                raise ValueError("must propagate")

    def test_closing_one_handle_does_not_close_a_sibling(self):
        first = Workspace.open(self.root)
        second = Workspace.open(self.root)
        self.addCleanup(second.close)
        first.close()
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        # The sibling still reaches packaged scripts: ``close`` must not have
        # torn down the process-wide assets root out from under it.
        self.assertTrue(hasattr(second._script("_script_errors"), "error_envelope"))

    def test_script_returns_a_packaged_module(self):
        with Workspace.open(self.root) as ws:
            module = ws._script("_script_errors")
            self.assertTrue(hasattr(module, "error_envelope"))
            self.assertIs(module, ws._script("_script_errors"))


class SharedAssetsRootTests(unittest.TestCase):
    """Two handles must enter ``resources.assets_root`` exactly once between them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_workspace(Path(self._tmp.name) / "ws")

    @contextlib.contextmanager
    def counting_assets_root(self):
        """Reset the process-wide root, count entries, then restore it.

        ``shared_assets_root`` memoizes on first use, so by the time this test
        runs another test may already have entered it and the counter would
        read zero for the wrong reason. Clearing the memo first is what makes
        the assertion mean what it says.
        """
        real = resources.assets_root
        entries = []

        @contextlib.contextmanager
        def counted():
            entries.append(1)
            with real() as root:
                yield root

        saved_stack = _script_host._SHARED_ASSETS_STACK
        saved_root = _script_host._SHARED_ASSETS_ROOT
        _script_host._SHARED_ASSETS_STACK = None
        _script_host._SHARED_ASSETS_ROOT = None
        resources.assets_root = counted
        try:
            yield entries
        finally:
            resources.assets_root = real
            # Release whatever this test caused to be entered, then hand the
            # process back the root the rest of the suite was already using.
            test_stack = _script_host._SHARED_ASSETS_STACK
            if test_stack is not None and test_stack is not saved_stack:
                test_stack.close()
            _script_host._SHARED_ASSETS_STACK = saved_stack
            _script_host._SHARED_ASSETS_ROOT = saved_root

    def test_two_handles_share_one_assets_root(self):
        with self.counting_assets_root() as entries:
            first = Workspace.open(self.root)
            second = Workspace.open(self.root)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            # Opening alone must not touch the assets at all: validation is
            # pure filesystem work on the workspace, not on the package assets.
            self.assertEqual(0, len(entries))

            module_one = first._script("_script_errors")
            module_two = second._script("_script_errors")
            self.assertEqual(1, len(entries), "the assets root was entered more than once")
            self.assertIs(module_one, module_two)

            # A third handle still rides the same entry.
            third = Workspace.open(self.root)
            self.addCleanup(third.close)
            third._script("_script_errors")
            self.assertEqual(1, len(entries))

    def test_a_handle_owns_no_exit_stack_of_its_own(self):
        # Per-handle ownership is what was rejected; ``__slots__`` makes the
        # absence of a stashed ExitStack checkable rather than merely intended.
        ws = Workspace.open(self.root)
        self.addCleanup(ws.close)
        self.assertFalse(hasattr(ws, "__dict__"), "the handle should stay __slots__-only")
        held = [getattr(ws, slot) for slot in Workspace.__slots__]
        self.assertEqual([], [value for value in held if isinstance(value, contextlib.ExitStack)])

    def test_close_leaves_the_shared_root_entered_for_the_rest_of_the_process(self):
        # The lifetime bug this guards against: a handle releasing a root that
        # is process-wide, stranding every other handle and the CLI with it.
        before = _script_host.shared_assets_root()
        ws = Workspace.open(self.root)
        ws._script("_script_errors")
        ws.close()
        self.assertIs(before, _script_host.shared_assets_root())
        self.assertEqual([], resources.missing_required_assets(before))


class FacadeSkeletonTests(unittest.TestCase):
    NAMESPACES = {
        "coverage": CoverageNamespace,
        "grounding": GroundingNamespace,
        "questions": QuestionsNamespace,
        "normalize": NormalizeNamespace,
        "orchestrate": OrchestrateNamespace,
        "diagnostics": DiagnosticsNamespace,
    }

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Workspace.open(make_workspace(Path(self._tmp.name) / "ws"))
        self.addCleanup(self.ws.close)

    def test_every_namespace_is_wired_onto_the_handle(self):
        for attribute, expected in self.NAMESPACES.items():
            with self.subTest(attribute=attribute):
                namespace = getattr(self.ws, attribute)
                self.assertIsInstance(namespace, expected)
                self.assertIsInstance(namespace, Namespace)
                self.assertIs(self.ws, namespace._workspace)
                self.assertEqual(self.ws.root, namespace._root)

    def test_each_namespace_lives_in_its_own_module(self):
        # Parallel work units fill different namespaces; one shared module would
        # put every one of them in every other one's diff.
        modules = {cls.__module__ for cls in self.NAMESPACES.values()}
        self.assertEqual(len(self.NAMESPACES), len(modules))
        self.assertEqual(
            {
                "evidence_wiki._facades.coverage",
                "evidence_wiki._facades.grounding",
                "evidence_wiki._facades.questions",
                "evidence_wiki._facades.normalize",
                "evidence_wiki._facades.orchestrate",
                "evidence_wiki._facades.diagnostics",
            },
            modules,
        )

    def test_namespaces_are_per_handle_not_shared_class_state(self):
        other = Workspace.open(self.ws.root)
        self.addCleanup(other.close)
        self.assertIsNot(self.ws.coverage, other.coverage)


class LazyReExportTests(unittest.TestCase):
    def test_importing_the_package_does_not_import_the_api_surface(self):
        # CLI startup imports ``evidence_wiki``; it must not pay for the facade
        # tree to do it. Checked in a clean interpreter because this test
        # process has already imported ``workspace`` above.
        import subprocess

        probe = (
            "import evidence_wiki, sys;"
            "eager=[n for n in ('evidence_wiki.workspace','evidence_wiki._facades','evidence_wiki.errors')"
            " if n in sys.modules];"
            "print(','.join(eager))"
        )
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO_ROOT),
            env={**__import__("os").environ, "PYTHONPATH": str(SRC_ROOT)},
        )
        self.assertEqual("", completed.stdout.strip(), "the package eagerly imported part of the API")

    def test_workspace_and_errors_resolve_lazily_through_getattr(self):
        self.assertIs(Workspace, evidence_wiki.Workspace)
        self.assertIs(errors, evidence_wiki.errors)

    def test_all_is_accurate_and_every_name_resolves(self):
        for name in evidence_wiki.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(evidence_wiki, name))
        self.assertIn("Workspace", evidence_wiki.__all__)
        self.assertIn("errors", evidence_wiki.__all__)
        self.assertIn("__version__", evidence_wiki.__all__)

    def test_unknown_attribute_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            _ = evidence_wiki.definitely_not_exported

    def test_dir_lists_the_lazy_names(self):
        listed = dir(evidence_wiki)
        self.assertIn("Workspace", listed)
        self.assertIn("errors", listed)


if __name__ == "__main__":
    unittest.main()
