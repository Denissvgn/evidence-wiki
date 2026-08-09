"""Seam conformance: a script's CLI and its library seam must never disagree.

`evidence-wiki` scripts are drivable two ways. A host can shell out to the script
and parse the JSON document it prints, or -- since CR-6 -- call the script's
``run_<op>(...) -> dict`` seam in-process. Those are two implementations of one
operation, and two implementations of one operation drift. This suite is the
permanent guard against that drift: for every enrolled script it runs the CLI and
the seam over the same inputs and requires them to agree, on the success document
and on the refusal alike. It is not a one-off check that a refactor landed
correctly; it is the reason a future change to either path cannot quietly move
only one of them.

`main` is run as a real subprocess on purpose. An in-process run would redirect
Python's own ``sys.stdout`` and miss the case this contract most needs to catch: a
child process inheriting the real stdout and writing to it.

What is asserted, per case:

- success -- the JSON document ``main`` printed on stdout equals the dict the seam
  returned;
- refusal -- the envelope ``main`` emitted on stderr equals ``to_envelope()`` on
  the ``ScriptRefusal`` the seam raised, and the process exit code equals the
  refusal's ``exit_code``.

Enrollment is automatic and per-file. A script that has a seam must have a case
module, so a new seam cannot quietly skip the contract.


The case-module contract
------------------------

Each enrolled script declares its cases in its own file at
``tests/seam_cases/<script_stem>.py``. That module must define:

``SCRIPT``
    The script's file name under ``workspace-template/scripts`` -- for example
    ``"workspace_status.py"``. It must match the case module's own stem.

``cases(workspace: Path) -> Sequence[SeamCase]``
    Called once, before any of this module's cases run, with a freshly
    initialized research workspace that belongs to this module alone. The
    function may prepare workspace state (claim a question, corrupt a config,
    copy the workspace aside) before returning its cases; ``workspace.parent`` is
    a scratch directory this module owns and may write to. Both a success case
    and a refusal case are required -- half the contract is the refusal.

A ``SeamCase`` (see ``tests/seam_cases/__init__.py``) pairs the argv the CLI is
invoked with against a callable that drives the seam with equivalent inputs::

    from pathlib import Path

    from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

    SCRIPT = "workspace_status.py"

    def cases(workspace: Path):
        root = str(workspace)
        return (
            SeamCase(
                name="status_document",
                argv=("--project-root", root, "--format", "json"),
                call=lambda module: module.run_status_report(root),
            ),
            SeamCase(
                name="unknown_run_id",
                argv=("--project-root", root, "--format", "json", "--run-id", "no-such-run"),
                call=lambda module: module.run_status_report(root, run_id="no-such-run"),
                expect=REFUSAL,
            ),
        )

Notes for whoever enrolls the next script:

- ``argv`` must name the workspace explicitly. The child process runs with its cwd
  set to the module's scratch directory, not to the workspace, so a case that
  relies on cwd fails loudly instead of passing by accident.
- ``call`` receives the script module already loaded in-process, and returns what
  the seam returns. Do not catch the refusal; let it propagate.
- Wall-clock fields are declared, not ignored: list them in ``volatile`` and they
  are required to be present on both sides before being blanked for the
  comparison.
- One case module per script, deliberately, rather than one shared case table.
  The purity suite next door keeps a single ``WORKSPACE_CASES`` dict, which is the
  right shape there; here several authors enroll their own script in parallel, and
  a new file conflicts with nobody while a shared dict would conflict with
  everybody.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from tests.seam_cases import REFUSAL, SUCCESS, SeamCase

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
CASES_DIR = Path(__file__).resolve().parent / "seam_cases"
CASES_PACKAGE = "tests.seam_cases"

# Defines ScriptRefusal for every script to raise; it is a shared helper, not a
# command, so it has no seam of its own and no case module.
NOT_A_SEAM = {"_script_errors.py"}

#: A seam is a top-level ``def run_<op>`` in a script that also speaks the shared
#: refusal. Both halves are needed: plenty of scripts have had top-level helpers
#: named ``run_*`` (``run_checks``, ``run_controller_section``, ``run_add``) since
#: long before CR-6, and naming alone would enroll all of them.
SEAM_DEFINITION = re.compile(r"^def run_\w+\(", re.MULTILINE)
REFUSAL_TYPE = "ScriptRefusal"

#: Substituted for a declared-volatile value on both sides before comparison.
VOLATILE = "<volatile>"


def enrolled_seam_scripts() -> set[str]:
    """Every script that defines a library seam, which is every script this contract binds."""
    enrolled: set[str] = set()
    for path in sorted(SCRIPTS.glob("*.py")):
        if path.name in NOT_A_SEAM:
            continue
        text = path.read_text(encoding="utf-8")
        if REFUSAL_TYPE in text and SEAM_DEFINITION.search(text):
            enrolled.add(path.name)
    return enrolled


def discovered_case_modules() -> dict[str, ModuleType]:
    """Import every ``tests/seam_cases/<stem>.py``, keyed by its stem."""
    modules: dict[str, ModuleType] = {}
    for path in sorted(CASES_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modules[path.stem] = importlib.import_module(f"{CASES_PACKAGE}.{path.stem}")
    return modules


def load_script_module(script: str) -> ModuleType:
    """Load one workspace script in-process, the way an embedding host would."""
    path = SCRIPTS / script
    spec = importlib.util.spec_from_file_location(f"seam_conformance_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def as_refusal(exc: Exception) -> Exception | None:
    """Return ``exc`` when it is a ScriptRefusal, recognised structurally.

    Class identity is not usable here: ``_workspace_module_loader`` deliberately
    hands some scripts their own isolated copy of ``_script_errors``, so two
    perfectly correct scripts can raise refusals of two different class objects.
    The codebase already identifies coded errors by their attributes elsewhere
    (``workspace_status.is_run_controller_error``); this follows that.
    """
    if callable(getattr(exc, "to_envelope", None)) and hasattr(exc, "exit_code"):
        return exc
    return None


def blank_volatile(document: Any, paths: tuple[str, ...]) -> tuple[Any, list[str]]:
    """Blank each declared volatile path, reporting any that was not there to blank."""
    normalized = copy.deepcopy(document)
    missing: list[str] = []
    for path in paths:
        keys = path.split(".")
        node = normalized
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and keys[-1] in node:
            node[keys[-1]] = VOLATILE
        else:
            missing.append(path)
    return normalized, missing


def initialize_workspace(target: Path, project_name: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "init_research_workspace.py"),
            "--target", str(target),
            "--project-name", project_name,
            "--project-description", "Seam conformance workspace.",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if not (target / "research.yml").is_file():
        raise AssertionError(f"could not initialize a seam conformance workspace: {result.stderr}")


@dataclass(frozen=True)
class Enrollment:
    """One case module, its workspace, and the cases it declared."""

    script: str
    scratch: Path
    cases: tuple[SeamCase, ...]


class SeamConformanceTests(unittest.TestCase):
    tmp: tempfile.TemporaryDirectory
    enrollments: dict[str, Enrollment]
    script_modules: dict[str, ModuleType]

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.script_modules = {}
        cls.enrollments = {}
        # Each case module gets its own workspace. Sharing one would make enrolling a
        # script a change to every other script's fixture, which is exactly the
        # coupling the per-file case modules exist to avoid.
        for stem, case_module in sorted(discovered_case_modules().items()):
            scratch = Path(cls.tmp.name) / stem
            scratch.mkdir(parents=True)
            workspace = scratch / "workspace"
            initialize_workspace(workspace, f"seam-{stem}")
            cls.enrollments[case_module.SCRIPT] = Enrollment(
                script=case_module.SCRIPT,
                scratch=scratch,
                cases=tuple(case_module.cases(workspace)),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def script_module(self, script: str) -> ModuleType:
        if script not in self.script_modules:
            self.script_modules[script] = load_script_module(script)
        return self.script_modules[script]

    def run_cli(self, enrollment: Enrollment, case: SeamCase) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / enrollment.script), *case.argv],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(enrollment.scratch),
        )

    # -- assertions --------------------------------------------------------------

    def check_success(self, enrollment: Enrollment, case: SeamCase, context: str) -> None:
        result = self.run_cli(enrollment, case)
        try:
            printed = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"{context}: stdout is not one JSON document ({exc}); stderr: {result.stderr[:400]!r}")

        try:
            returned = case.call(self.script_module(enrollment.script))
        except Exception as exc:
            refusal = as_refusal(exc)
            if refusal is None:
                raise
            self.fail(f"{context}: the seam refused an operation the CLI completed: {refusal}")

        printed_normalized, printed_missing = blank_volatile(printed, case.volatile)
        returned_normalized, returned_missing = blank_volatile(returned, case.volatile)
        self.assertEqual([], printed_missing, f"{context}: CLI document is missing declared volatile paths")
        self.assertEqual([], returned_missing, f"{context}: seam document is missing declared volatile paths")
        self.assertEqual(
            printed_normalized,
            returned_normalized,
            f"{context}: the seam returned a different document than the CLI printed",
        )

    def check_refusal(self, enrollment: Enrollment, case: SeamCase, context: str) -> None:
        result = self.run_cli(enrollment, case)
        try:
            returned = case.call(self.script_module(enrollment.script))
        except Exception as exc:
            refusal = as_refusal(exc)
            if refusal is None:
                raise
        else:
            self.fail(f"{context}: the seam returned {returned!r} where the CLI refused")

        try:
            emitted = json.loads(result.stderr)
        except json.JSONDecodeError as exc:
            self.fail(f"{context}: stderr is not the error envelope ({exc}): {result.stderr[:400]!r}")
        self.assertEqual(
            emitted,
            refusal.to_envelope(),
            f"{context}: the envelope the CLI emitted differs from the one the seam raised",
        )
        self.assertEqual(
            result.returncode,
            refusal.exit_code,
            f"{context}: the CLI exit code differs from the refusal's exit_code",
        )

    # -- the contract ------------------------------------------------------------

    def test_every_seam_script_is_enrolled(self):
        """A new seam cannot skip the contract by not being declared."""
        self.assertEqual(
            enrolled_seam_scripts(),
            {module.SCRIPT for module in discovered_case_modules().values()},
            "a script defining a run_<op> seam needs tests/seam_cases/<stem>.py "
            "(or an entry in NOT_A_SEAM with a reason)",
        )

    def test_case_modules_are_named_after_their_script(self):
        """``tests/seam_cases/<stem>.py`` covers ``<stem>.py``, so no two modules claim one script."""
        for stem, module in sorted(discovered_case_modules().items()):
            with self.subTest(case_module=stem):
                self.assertEqual(f"{stem}.py", module.SCRIPT)

    def test_every_script_declares_both_outcomes(self):
        """Half of the seam contract is the refusal, so every script exercises both."""
        for script, enrollment in sorted(self.enrollments.items()):
            with self.subTest(script=script):
                outcomes = {case.expect for case in enrollment.cases}
                self.assertIn(SUCCESS, outcomes, f"{script}: declare at least one success case")
                self.assertIn(REFUSAL, outcomes, f"{script}: declare at least one refusal case")

    def test_the_seam_and_the_cli_agree(self):
        for script, enrollment in sorted(self.enrollments.items()):
            for case in enrollment.cases:
                with self.subTest(script=script, case=case.name):
                    context = f"{script} [{case.name}]"
                    if case.note:
                        context += f" ({case.note})"
                    self.assertIn(case.expect, (SUCCESS, REFUSAL), f"{context}: unknown expect")
                    if case.expect == SUCCESS:
                        self.check_success(enrollment, case, context)
                    else:
                        self.check_refusal(enrollment, case, context)


if __name__ == "__main__":
    unittest.main()
