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
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import evidence_wiki
from evidence_wiki import _contract as contract_module
from evidence_wiki import cli, domain_pack_validator, resources
from evidence_wiki._script_host import load_packaged_script


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
        self.assertEqual({"version", "surface", "matrix_version", "operations", "cli_only", "timeout_policy"}, set(self.block))
        self.assertEqual("9", self.block["version"])
        self.assertEqual(contract_module.LIBRARY_API_VERSION, self.block["version"])
        # A JSON payload cannot carry the declaration tuple, so the surface has to
        # arrive as a list on both sides of the CLI boundary.
        self.assertIsInstance(self.block["surface"], list)
        self.assertEqual(list(contract_module.LIBRARY_API_SURFACE), self.block["surface"])

    def test_the_surface_names_every_declared_operation_exactly_once(self):
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
            "workspace.publish_selected",
            "workspace.doctor",
            "coverage.evaluate",
            "grounding.verify",
            "normalize.verify",
            "normalize.profiles",
            "normalize.validate_packet",
            "normalize.validate_execution",
            "normalize.validate_market",
            "usage.status",
            "usage.transact",
            "usage.check",
            "usage.lineage",
            "usage.materialize",
            "snapshots.prepare",
            "snapshots.export",
            "snapshots.check",
            "temporal.evaluate",
            "verify_snapshot",
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

    def test_every_declared_operation_resolves_and_has_one_boundary(self):
        from evidence_wiki import Workspace, _facades

        owners = {
            "workspace": Workspace, "coverage": _facades.CoverageNamespace,
            "grounding": _facades.GroundingNamespace, "normalize": _facades.NormalizeNamespace,
            "questions": _facades.QuestionsNamespace, "orchestrate": _facades.OrchestrateNamespace,
            "orchestrate.session": _facades.OrchestrationSession,
            "usage": _facades.UsageNamespace,
            "snapshots": _facades.SnapshotsNamespace,
            "temporal": _facades.TemporalNamespace,
        }
        boundaries = self.block["operations"]
        self.assertEqual(len(self.block["surface"]), len(boundaries))
        self.assertEqual(set(self.block["surface"]), {row["operation"] for row in boundaries})
        self.assertFalse(set(self.block["surface"]) & {row["operation"] for row in self.block["cli_only"]})
        for row in boundaries:
            name = row["operation"]
            with self.subTest(operation=name):
                owner, _, attribute = name.rpartition(".")
                self.assertTrue(callable(getattr(owners.get(owner, evidence_wiki), attribute)))
                for field in ("mutation", "locking", "subprocess"):
                    self.assertTrue(row[field])

    def test_operation_boundaries_are_caller_owned(self):
        first = evidence_wiki.contract()
        first["library_api"]["operations"][0]["mutation"] = "changed by caller"
        self.assertNotEqual("changed by caller", evidence_wiki.contract()["library_api"]["operations"][0]["mutation"])


class PackPolicyRulesTests(unittest.TestCase):
    """`_pack_policy_rules` (CR-9 T6): the `policy_rules` block published beside
    `policy_vocabulary_definitions`.

    Exercised directly against the injected-parameter helper rather than through a
    full `evidence_wiki.contract()` call, mirroring how the module was written to be
    tested: ``root`` names an arbitrary ``domain-packs`` directory, decoupled from
    where ``policy_primitives_module`` itself was loaded from, so these cases never
    have to touch the real ``domain-packs/`` tree that ships with the package.
    """

    @classmethod
    def setUpClass(cls):
        with resources.assets_root() as root:
            cls.policy_primitives_module = load_packaged_script(root, "_policy_primitives")

    @staticmethod
    def _root_with_pack(tmpdir: Path, overlay_document: dict) -> Path:
        pack_dir = tmpdir / "domain-packs" / "temp-pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "research.overlay.yml").write_text(
            yaml.safe_dump(overlay_document), encoding="utf-8"
        )
        return tmpdir

    def test_no_domain_packs_directory_yields_an_empty_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = contract_module._pack_policy_rules(Path(tmpdir), self.policy_primitives_module, yaml)
        self.assertEqual({}, result)

    def test_a_pack_declaring_no_rules_is_absent_from_the_block(self):
        # Same posture as `_pack_policy_vocabularies`: a pack that declares nothing
        # relevant is left out entirely rather than published as an empty entry.
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "freshness_policy": {
                        "pack:temp-pack/quote-48h": "A supplier quote must be at most 48 hours old.",
                    },
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            result = contract_module._pack_policy_rules(root, self.policy_primitives_module, yaml)
        self.assertEqual({}, result)

    def test_a_populated_rule_reports_its_primitives_and_review_flag(self):
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "freshness_policy": {
                        "pack:temp-pack/quote-48h": "A supplier quote must be at most 48 hours old.",
                    },
                },
                "policy_rules": {
                    "pack:temp-pack/quote-48h": {
                        "manual_review_required": True,
                        "all_of": [
                            {"max_age": {"field": "provenance/retrieved_at", "hours": 48}},
                        ],
                    },
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            result = contract_module._pack_policy_rules(root, self.policy_primitives_module, yaml)
        self.assertEqual(
            {
                "temp-pack": {
                    "pack:temp-pack/quote-48h": {
                        "primitives": ["all_of", "max_age"],
                        "manual_review_required": True,
                        "manual_review_on_absence": False,
                        "record_fields_that_may_traverse_arrays": [],
                        "section": "freshness_policy",
                    },
                },
            },
            result,
        )

    def test_primitive_names_are_sorted_and_deduplicated_across_a_nested_composition(self):
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "identity_policy": {
                        "pack:temp-pack/sku-matches": "The quoted SKU must match the candidate.",
                    },
                },
                "policy_rules": {
                    "pack:temp-pack/sku-matches": {
                        "any_of": [
                            {
                                "all_of": [
                                    {
                                        "equals": {
                                            "field": "record/supplier_quote/sku",
                                            "question_field": "metadata/candidate_sku",
                                        }
                                    },
                                    {"one_of_provenance": {"providers": ["aliexpress-ds"]}},
                                ]
                            },
                            {"one_of_provenance": {"providers": ["partner-catalog"]}},
                        ],
                    },
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            result = contract_module._pack_policy_rules(root, self.policy_primitives_module, yaml)
        entry = result["temp-pack"]["pack:temp-pack/sku-matches"]
        self.assertEqual(["all_of", "any_of", "equals", "one_of_provenance"], entry["primitives"])
        self.assertFalse(entry["manual_review_required"])
        self.assertFalse(entry["manual_review_on_absence"])

    def test_conditional_review_summary_is_published_by_the_installation_contract(self):
        policy = "pack:temp-pack/sku-optional"
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "identity_policy": {policy: "Compare the optional quoted SKU when present."},
                },
                "policy_rules": {
                    policy: {
                        "all_of": [
                            {
                                "equals": {
                                    "field": "record/supplier_quote/sku",
                                    "value": "B0ABC12345",
                                    "when_absent": "manual_review",
                                }
                            }
                        ]
                    },
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            result = contract_module._pack_policy_rules(root, self.policy_primitives_module, yaml)

        self.assertEqual(
            {
                "primitives": ["all_of", "equals"],
                "manual_review_required": False,
                "manual_review_on_absence": True,
                "record_fields_that_may_traverse_arrays": [],
                "section": "identity_policy",
            },
            result["temp-pack"][policy],
        )

    def test_pack_validator_and_installation_contract_publish_the_same_rule_summary(self):
        policy = "pack:temp-pack/sku-optional"
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "identity_policy": {policy: "Compare the optional quoted SKU when present."},
                },
                "policy_rules": {
                    policy: {
                        "all_of": [
                            {
                                "equals": {
                                    "field": "record/supplier_quote/sku",
                                    "value": "B0ABC12345",
                                    "when_absent": "manual_review",
                                }
                            }
                        ]
                    },
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            contract_rules = contract_module._pack_policy_rules(
                root, self.policy_primitives_module, yaml
            )["temp-pack"]
        validator_rules, validator_check = domain_pack_validator.policy_rules_check(
            SimpleNamespace(policy_primitives=self.policy_primitives_module),
            overlay["domain_pack"],
        )

        self.assertEqual("pass", validator_check["status"], validator_check)
        self.assertEqual(validator_rules, contract_rules)

    def test_a_malformed_rule_is_skipped_rather_than_raised(self):
        # `not_a_real_primitive` is not in `PRIMITIVE_NAMES`, so `pack_policy_rules`
        # raises `PolicyRuleError`; the walk must catch exactly that type and move on,
        # the same posture `_pack_policy_vocabularies` takes on `CoverageManifestError`.
        overlay = {
            "domain_pack": {
                "name": "temp-pack",
                "policy_vocabularies": {
                    "freshness_policy": {
                        "pack:temp-pack/quote-48h": "A supplier quote must be at most 48 hours old.",
                    },
                },
                "policy_rules": {
                    "pack:temp-pack/quote-48h": {"all_of": [{"not_a_real_primitive": {}}]},
                },
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._root_with_pack(Path(tmpdir), overlay)
            result = contract_module._pack_policy_rules(root, self.policy_primitives_module, yaml)
        self.assertEqual({}, result)

    def test_the_block_is_reachable_through_the_full_contract_payload(self):
        payload = evidence_wiki.contract()
        self.assertIn("policy_rules", payload)
        rules = payload["policy_rules"]["capital-markets"]
        self.assertEqual(6, len(rules))
        self.assertTrue(all(key.startswith("pack:capital-markets/") for key in rules))
        self.assertTrue(rules["pack:capital-markets/listing-review"]["manual_review_required"])


if __name__ == "__main__":
    unittest.main()
