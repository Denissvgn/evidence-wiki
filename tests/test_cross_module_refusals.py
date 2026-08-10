"""Refusals must survive the module-isolation boundary between workspace scripts.

``_workspace_module_loader`` isolates every sibling stem on each load,
``_script_errors`` included, so each script that goes through the loader gets its
*own* ``ScriptRefusal`` class object. Two consequences follow, and this suite pins
both of them.

First, ``isinstance`` is not a reliable test across that boundary: a refusal raised
inside ``coverage_manifest`` is not an instance of the ``ScriptRefusal`` that
``export_answers`` imported. An ``except ScriptRefusal`` arm wrapping a *sibling*
call would therefore catch nothing while reading exactly as though it handled the
case -- the failure mode is a traceback where an error envelope was intended.

Second, the code is nonetheless correct today, which is why no existing test fails:
every cross-script catch names the sibling's own attribute
(``except verify_quotes.VerifyQuotesError``), which is the class object that
actually exists on the other side. That correctness is unenforced, though. It holds
because each author remembered, and nothing would notice if the next one did not.
``test_cross_script_refusal_catches_are_module_qualified`` is what notices.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"

#: Refusal types that cross a module boundary when raised. Catching one of these by
#: bare name only works inside the file that defines it.
REFUSAL_TYPES = frozenset(
    {
        "ScriptRefusal",
        "ClaimError",
        "VerifyQuotesError",
        "CoverageManifestError",
        "NormalizeVerifyError",
        "ExportRefusal",
        "IntakeValidationError",
        "ResolveError",
    }
)


def load_script(stem: str):
    """Load one workspace script the way the package loads it: sibling-isolated."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        from _workspace_module_loader import load_workspace_module

        return load_workspace_module(SCRIPTS, stem)
    finally:
        sys.path.remove(str(SCRIPTS))


def locally_defined_exceptions(tree: ast.Module) -> set[str]:
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


class ClassIdentityTests(unittest.TestCase):
    """The premise: the same class name is a different object per loaded script."""

    def test_each_script_gets_its_own_refusal_class(self):
        coverage = load_script("coverage_manifest")
        export = load_script("export_answers")
        self.assertIsNot(
            coverage._script_errors.ScriptRefusal,
            export.ScriptRefusal,
            "module isolation is what makes structural recognition necessary; if these "
            "ever become the same object the loader changed and this suite's premise "
            "should be re-derived rather than deleted",
        )

    def test_isinstance_fails_across_the_boundary(self):
        coverage = load_script("coverage_manifest")
        export = load_script("export_answers")
        refusal = coverage.CoverageManifestError("X_CODE", "boom")
        self.assertNotIsInstance(refusal, export.ScriptRefusal)

    def test_is_refusal_recognizes_a_foreign_refusal(self):
        """The predicate that works where ``isinstance`` does not."""
        coverage = load_script("coverage_manifest")
        export = load_script("export_answers")
        foreign = coverage.CoverageManifestError("X_CODE", "boom")
        self.assertTrue(export.is_refusal(foreign))
        # And it stays honest about things that merely resemble one.
        self.assertFalse(export.is_refusal(ValueError("boom")))
        self.assertFalse(export.is_refusal(None))
        self.assertFalse(export.is_refusal(SystemExit("plain funnel")))

    def test_a_foreign_refusal_still_renders(self):
        """``emit_refusal`` reads the shape, so a foreign refusal needs no conversion."""
        coverage = load_script("coverage_manifest")
        foreign = coverage.CoverageManifestError("X_CODE", "boom")
        envelope = foreign.to_envelope()
        self.assertEqual("X_CODE", envelope["error_code"])
        self.assertEqual("boom", envelope["message"])


class ExportReachabilityTests(unittest.TestCase):
    """The reachable path the hazard was reported against, driven end to end.

    ``build_export`` calls ``verify_quotes.grounding_entries``, which raises
    ``VerifyQuotesError`` on frontmatter a human can edit. If that refusal escaped,
    ``evidence-wiki export`` would traceback on a malformed question page.
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.workspace = root / "ws"
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "evidence_wiki.cli",
                "init",
                "--target",
                str(cls.workspace),
                "--project-name",
                "cross-module",
                "--project-description",
                "cross-module refusal probe",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
        )
        batch = root / "batch.yaml"
        batch.write_text(
            'schema_version: "1.0"\nquestions:\n  - question: What matters?\n'
            "    id: benchmarks\n    priority: high\n",
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "evidence_wiki.cli",
                "questions",
                "add",
                "--target",
                str(cls.workspace),
                "--from-file",
                str(batch),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_malformed_grounding_is_a_warning_not_a_traceback(self):
        page = self.workspace / "wiki" / "questions" / "benchmarks.md"
        original = page.read_text(encoding="utf-8")
        # A string where the contract requires a list: the shape violation
        # ``grounding_entries`` refuses on.
        page.write_text(
            original.replace("status: open", "status: answered\ngrounding: not-a-list", 1),
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "evidence_wiki.cli",
                    "export",
                    "--target",
                    str(self.workspace),
                    "--format",
                    "json",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            page.write_text(original, encoding="utf-8")

        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(0, completed.returncode, completed.stderr)
        document = json.loads(completed.stdout)
        self.assertTrue(
            [w for w in document.get("warnings", []) if "grounding" in w.lower()],
            "the sibling's refusal should reach the export document as a warning",
        )


class InvariantGuardTests(unittest.TestCase):
    """What keeps the next author from reintroducing the hazard."""

    def test_cross_script_refusal_catches_are_module_qualified(self):
        """A refusal type caught by bare name must be defined in that same file.

        Catching ``except SomeError`` binds the name this module imported or defined.
        That is right for a refusal the module raises itself and wrong for one a
        sibling raises, because the sibling's class object is a different object.
        A cross-script catch must therefore name the sibling
        (``except sibling.SomeError``) or sort on shape (``is_refusal``).
        """
        offenders: list[str] = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            local = locally_defined_exceptions(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler) or node.type is None:
                    continue
                caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
                for item in caught:
                    # ``except sibling.SomeError`` is an Attribute and always fine:
                    # it resolves the class off the module object that raised it.
                    if not isinstance(item, ast.Name) or item.id not in REFUSAL_TYPES:
                        continue
                    if item.id not in local and item.id != "ScriptRefusal":
                        offenders.append(f"{path.name}:{item.lineno} bare `except {item.id}`")
        self.assertEqual(
            [],
            offenders,
            "a refusal type caught by bare name must be defined in the same file; "
            "for a sibling's refusal catch `sibling.TheError` or use `is_refusal`",
        )

    def test_bare_script_refusal_does_not_wrap_a_sibling_call(self):
        """``except ScriptRefusal`` must not guard a call into a sibling module.

        The previous test cannot see this one: ``ScriptRefusal`` is imported by every
        seamed script, so catching it by bare name is legitimate -- for refusals the
        module raises *itself*. It becomes wrong the moment the guarded block calls a
        sibling, because the refusal that arrives then belongs to the sibling's class
        object, not this module's, and the arm silently stops catching.

        Sibling modules are recognized by the loader calls that produce them, so this
        follows the real data flow rather than a naming convention.
        """
        offenders: list[str] = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            sibling_vars = {
                target.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id in {"load_sibling_module", "load_workspace_module"}
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            if not sibling_vars:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                catches_bare = any(
                    isinstance(item, ast.Name) and item.id == "ScriptRefusal"
                    for handler in node.handlers
                    if handler.type is not None
                    for item in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])
                )
                if not catches_bare:
                    continue
                touched = sorted(
                    {
                        inner.value.id
                        for statement in node.body
                        for inner in ast.walk(statement)
                        if isinstance(inner, ast.Attribute)
                        and isinstance(inner.value, ast.Name)
                        and inner.value.id in sibling_vars
                    }
                )
                if touched:
                    offenders.append(f"{path.name}:{node.lineno} `except ScriptRefusal` guards {', '.join(touched)}")
        self.assertEqual(
            [],
            offenders,
            "catch the sibling's own refusal type (`except sibling.TheError`) or sort on "
            "shape with `is_refusal`; a bare `except ScriptRefusal` cannot see it",
        )

    def test_is_refusal_is_exported_for_scripts_that_need_it(self):
        script_errors = load_script("_script_errors")
        self.assertTrue(callable(script_errors.is_refusal))


if __name__ == "__main__":
    unittest.main()


class PassThroughArmTests(unittest.TestCase):
    """A seam that passes refusals through must recognize them by shape.

    Three seams once opened with ``except ScriptRefusal: raise`` under a comment
    promising that a *sibling* seam's refusal would be handed on untouched. None
    of them could do it: the arm binds the catching module's own ``ScriptRefusal``,
    and a sibling's is a different class object, so a foreign refusal fell past it
    into whatever generic handler came next and was re-wrapped -- keeping the
    envelope but losing the ``text_line`` the pass-through existed to preserve.

    An ``except`` clause cannot name "any script's refusal", so the arms now sort
    on :func:`_script_errors.is_refusal` instead. This guard keeps them that way.
    """

    #: A bare pass-through arm: ``except ScriptRefusal`` whose body is just ``raise``.
    def bare_pass_through_arms(self, tree: ast.Module) -> list[int]:
        found: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.type is None:
                continue
            caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            names = {item.id for item in caught if isinstance(item, ast.Name)}
            if "ScriptRefusal" not in names:
                continue
            body = [stmt for stmt in node.body if not isinstance(stmt, ast.Pass)]
            if len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is None:
                found.append(node.lineno)
        return found

    def test_no_seam_passes_refusals_through_by_class_name(self):
        offenders: list[str] = []
        for path in sorted(SCRIPTS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            offenders.extend(f"{path.name}:{line}" for line in self.bare_pass_through_arms(tree))
        self.assertEqual(
            [],
            offenders,
            "`except ScriptRefusal: raise` only passes through this module's own refusals; "
            "sort on `is_refusal(exc)` so a sibling's refusal is handed on with its text_line",
        )

    def test_a_foreign_refusal_survives_a_seam_untouched(self):
        """The behaviour the guard above protects, asserted end to end."""
        status = load_script("workspace_status")
        coverage = load_script("coverage_manifest")
        foreign = coverage.CoverageManifestError("COVERAGE_MANIFEST_INVALID", "sibling refused", exit_code=2)

        # Not catchable by class across the boundary, but recognizable by shape.
        self.assertNotIsInstance(foreign, status.ScriptRefusal)
        self.assertTrue(status.is_refusal(foreign))

        original = status.cached_status_document
        status.cached_status_document = lambda *a, **k: (_ for _ in ()).throw(foreign)
        try:
            with self.assertRaises(type(foreign)) as caught:
                status.run_status_report(".")
        finally:
            status.cached_status_document = original
        self.assertIs(foreign, caught.exception, "the seam re-wrapped a refusal it should have passed on")
        self.assertEqual("sibling refused", caught.exception.text_line)
