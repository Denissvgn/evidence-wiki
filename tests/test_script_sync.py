"""Guard against drift between the starter scripts and vendored workspace copies.

``workspace-template/scripts/`` is the single source of truth for workspace
tooling. Worked examples ship a ``scripts/`` directory so they resemble a real
initialized workspace, but those copies are pure config-driven tooling with no
per-workspace customization, so they must stay byte-identical to the template.
This test fails the moment a tracked example drifts, which previously let stale,
less-safe scripts ship in published examples.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_SCRIPTS_DIR = REPO_ROOT / "workspace-template" / "scripts"
EXAMPLES_DIR = REPO_ROOT / "examples"


def template_scripts() -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(TEMPLATE_SCRIPTS_DIR.glob("*.py"))}


def vendored_script_dirs() -> list[Path]:
    dirs: list[Path] = []
    if EXAMPLES_DIR.is_dir():
        for workspace in sorted(EXAMPLES_DIR.glob("*")):
            scripts_dir = workspace / "scripts"
            if scripts_dir.is_dir():
                dirs.append(scripts_dir)
    return dirs


class ScriptSyncTests(unittest.TestCase):
    def test_template_has_scripts(self):
        self.assertTrue(template_scripts(), "template scripts directory must contain at least one script")

    def test_examples_exist_to_guard(self):
        self.assertTrue(vendored_script_dirs(), "expected at least one example workspace with a scripts/ directory")

    def test_example_scripts_are_byte_identical_to_template(self):
        expected = template_scripts()
        for scripts_dir in vendored_script_dirs():
            relative = scripts_dir.relative_to(REPO_ROOT).as_posix()
            present = {path.name for path in scripts_dir.glob("*.py")}
            for name, content in expected.items():
                target = scripts_dir / name
                with self.subTest(workspace=relative, script=name):
                    self.assertTrue(
                        target.is_file(),
                        f"{relative}/{name} missing; run tools/sync_vendored_scripts.py",
                    )
                    self.assertEqual(
                        target.read_bytes(),
                        content,
                        f"{relative}/{name} drifted from the template; run tools/sync_vendored_scripts.py",
                    )
            extras = sorted(present - set(expected))
            self.assertEqual(
                extras,
                [],
                f"{relative} has scripts not present in the template: {extras}",
            )


if __name__ == "__main__":
    unittest.main()
