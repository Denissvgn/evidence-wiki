"""Prove the capability contract is one payload with two front ends.

The payload used to be built inside :mod:`evidence_wiki.cli`, which meant an
embedding host could only read it by spawning ``evidence-wiki contract`` and
parsing stdout. It now lives in :mod:`evidence_wiki._contract` and is reachable
as ``evidence_wiki.contract()``. The anti-drift test here is what makes that move
safe: whatever the CLI prints must parse back to exactly the dict the library
returns, so a future edit cannot quietly grow a second payload builder.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import evidence_wiki
from evidence_wiki import _contract as contract_module
from evidence_wiki import cli


def run_cli_contract() -> str:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = cli.main(["contract"])
    if exit_code:
        raise AssertionError(f"contract subcommand exited {exit_code}")
    return stdout.getvalue()


def run_python(code: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(SRC_ROOT) if not existing else os.pathsep.join([str(SRC_ROOT), existing])
    return subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


class ContractParityTests(unittest.TestCase):
    def test_library_entry_point_equals_what_the_cli_prints(self):
        printed = run_cli_contract()

        self.assertEqual(json.loads(printed), evidence_wiki.contract())
        # The formatting is part of the CLI's contract with shell consumers, so
        # re-serializing the library payload has to reproduce it byte for byte.
        self.assertEqual(printed, json.dumps(evidence_wiki.contract(), indent=2, sort_keys=False) + "\n")

    def test_library_entry_point_equals_a_separately_spawned_cli_process(self):
        result = run_python(
            "import json, sys\n"
            "from evidence_wiki import cli\n"
            "sys.exit(cli.main(['contract']))\n"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(json.loads(result.stdout), evidence_wiki.contract())

    def test_cli_keeps_the_payload_builder_under_its_original_name(self):
        self.assertIs(contract_module.contract, cli._contract_payload)
        self.assertEqual(contract_module.CONTRACT_SCHEMA_VERSION, cli.CONTRACT_SCHEMA_VERSION)
        self.assertEqual(cli.CONTRACT_SCHEMA_VERSION, evidence_wiki.contract()["schema_version"])

    def test_the_package_attribute_stays_the_callable_across_repeated_access(self):
        # The payload module is private precisely so this holds: a submodule named
        # ``contract`` would be bound onto the package by the import system the
        # first time anything imported it, and would then shadow the callable for
        # every later lookup.
        first = evidence_wiki.contract
        second = evidence_wiki.contract
        self.assertIs(first, second)
        self.assertIs(first, contract_module.contract)
        self.assertTrue(callable(evidence_wiki.contract))

    def test_the_callable_survives_importing_the_cli_in_either_order(self):
        for order in ("package first", "cli first"):
            with self.subTest(order=order):
                statements = (
                    "import evidence_wiki\n"
                    "evidence_wiki.contract\n"
                    "import evidence_wiki.cli\n"
                    if order == "package first"
                    else "import evidence_wiki.cli\nimport evidence_wiki\n"
                )
                result = run_python(statements + "print(type(evidence_wiki.contract()).__name__)\n")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("dict", result.stdout.strip())

    def test_unknown_package_attributes_still_raise_attribute_error(self):
        with self.assertRaises(AttributeError):
            evidence_wiki.definitely_not_exported  # noqa: B018

    def test_importing_the_package_does_not_build_the_contract(self):
        result = run_python(
            "import sys\n"
            "import evidence_wiki\n"
            "loaded = [name for name in ('evidence_wiki._contract', 'evidence_wiki.cli',"
            " 'evidence_wiki.orchestration') if name in sys.modules]\n"
            "print(repr(loaded))\n"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[]", result.stdout.strip())

    def test_the_payload_is_caller_owned_on_every_call(self):
        payload = evidence_wiki.contract()
        payload["library_api"]["surface"].clear()
        payload["artifact_schema_documents"]["orchestration_session"]["required"].clear()

        rebuilt = evidence_wiki.contract()
        self.assertTrue(rebuilt["library_api"]["surface"])
        self.assertTrue(rebuilt["artifact_schema_documents"]["orchestration_session"]["required"])


class LibraryApiNegotiationBlockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.block = evidence_wiki.contract()["library_api"]

    def test_the_block_is_version_gated_rather_than_introspected(self):
        self.assertEqual({"version", "surface"}, set(self.block))
        self.assertEqual("1", self.block["version"])
        self.assertEqual(contract_module.LIBRARY_API_VERSION, self.block["version"])
        # A JSON payload cannot carry the declaration tuple, so the surface has to
        # arrive as a list on both sides of the CLI boundary.
        self.assertIsInstance(self.block["surface"], list)
        self.assertEqual(list(contract_module.LIBRARY_API_SURFACE), self.block["surface"])

    def test_the_surface_names_every_v1_operation_exactly_once(self):
        surface = self.block["surface"]
        self.assertEqual(sorted(set(surface)), sorted(surface))
        for name in surface:
            with self.subTest(name=name):
                self.assertIsInstance(name, str)
                self.assertEqual(name, name.strip())
                self.assertTrue(name)

        expected = {
            "workspace.open",
            "workspace.close",
            "workspace.versions",
            "workspace.status",
            "workspace.export_answers",
            "workspace.doctor",
            "coverage.evaluate",
            "grounding.verify",
            "normalize.verify",
            "orchestrate.start",
            "orchestrate.session.next",
            "orchestrate.session.submit",
            "orchestrate.session.status",
            "fleet_status",
            "contract",
        }
        expected.update(
            f"questions.{operation}"
            for operation in (
                "claim",
                "release",
                "answer",
                "block",
                "defer",
                "reject",
                "reopen",
                "approve",
                "review",
                "set_grounding",
                "add_batch",
            )
        )
        self.assertEqual(expected, set(surface))

    def test_the_block_negotiates_beside_the_other_capability_blocks(self):
        payload = evidence_wiki.contract()
        # An orchestrator negotiates the API the same way it negotiates artifact
        # schema versions, so the block has to travel in the same document.
        self.assertIn("library_api", payload)
        self.assertIn("orchestration_capabilities", payload)
        self.assertIn("artifact_schemas", payload)


if __name__ == "__main__":
    unittest.main()
