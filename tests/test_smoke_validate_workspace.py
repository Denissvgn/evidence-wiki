import contextlib
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "init_research_workspace.py"
SMOKE_SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "smoke_validate_workspace.py"
STATUS_SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "workspace_status.py"
CONTROLLER_SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "orchestration_controller.py"

sys.path.insert(0, str(REPO_ROOT))
from tests._provider_plugin_fixture import (  # noqa: E402
    ACQUISITION_PROVIDER_ID,
    DISCOVERY_PROVIDER_ID,
    installed_provider_plugins,
    refresh_provider_plugin_caches,
)


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("research_workspace_init_for_smoke_tests", INIT_SCRIPT_PATH)
SMOKE = load_script_module("research_workspace_smoke_validate", SMOKE_SCRIPT_PATH)


class SmokeValidateWorkspaceTests(unittest.TestCase):
    def create_workspace(self, root: Path, *extra_args: str) -> Path:
        target = root / "workspace"
        args = [
            "--target",
            str(target),
            "--project-name",
            "smoke-workspace",
            "--project-description",
            "Workspace created for smoke validation tests.",
            *extra_args,
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = INIT.main(args)
        self.assertEqual(0, exit_code)
        return target

    def run_smoke_cli(self, target: Path, output_format: str = "text") -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = SMOKE.main(["--project-root", str(target), "--format", output_format])
        return exit_code, stdout.getvalue()

    def issue_categories(self, results: dict) -> set[str]:
        return {issue["category"] for issue in results["issues"]}

    def load_config(self, target: Path) -> dict:
        return yaml.safe_load((target / "research.yml").read_text())

    def write_config(self, target: Path, config: dict) -> None:
        (target / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    def test_clean_generated_workspace_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))

            results = SMOKE.run_checks(target)
            exit_code, output = self.run_smoke_cli(target)

            self.assertTrue(results["ok"])
            self.assertEqual([], results["issues"])
            self.assertEqual(0, exit_code)
            self.assertIn("Smoke validation passed.", output)
            self.assertTrue((target / "scripts" / "smoke_validate_workspace.py").is_file())

    def test_domain_pack_workspace_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir), "--domain-pack", "llm-research")

            results = SMOKE.run_checks(target)

            self.assertTrue(results["ok"])
            self.assertEqual([], results["issues"])

    def test_forbidden_acquisition_automation_key_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            config = self.load_config(target)
            config["integrations"]["acquisition"]["auto_fetch"] = True
            self.write_config(target, config)

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertIn("integration_safety", self.issue_categories(results))
            self.assertTrue(
                any(issue.get("field") == "integrations.acquisition.auto_fetch" for issue in results["issues"]),
                results["issues"],
            )

    def test_enabled_discovery_requires_concrete_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            config = self.load_config(target)
            config["integrations"]["discovery"] = {
                "enabled": True,
                "providers": [],
                "candidate_store_path": "sources/discovery/candidates.jsonl",
            }
            self.write_config(target, config)

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertTrue(
                any(
                    issue.get("field") == "integrations.discovery.providers"
                    and issue["severity"] == "HIGH"
                    for issue in results["issues"]
                ),
                results["issues"],
            )

    def test_legacy_discovery_strategy_is_low_and_does_not_satisfy_provider_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            (target / "sources" / "discovery").mkdir()
            config = self.load_config(target)
            config["integrations"]["discovery"] = {
                "enabled": True,
                "providers": ["legal"],
                "candidate_store_path": "sources/discovery/candidates.jsonl",
            }
            self.write_config(target, config)

            results = SMOKE.run_checks(target)

            legacy = [
                issue
                for issue in results["issues"]
                if issue.get("category") == "deprecated_config"
            ]
            self.assertEqual(1, len(legacy), results["issues"])
            self.assertEqual("LOW", legacy[0]["severity"])
            self.assertFalse(results["ok"], results["issues"])
            self.assertTrue(
                any(
                    issue.get("field") == "integrations.discovery.providers"
                    and issue["severity"] == "HIGH"
                    for issue in results["issues"]
                ),
                results["issues"],
            )

    def test_missing_required_wiki_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            shutil.rmtree(target / "wiki" / "systems")

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertIn("configured_directory", self.issue_categories(results))
            self.assertTrue(any("wiki/systems" in issue["files"] for issue in results["issues"]))

    def test_missing_agent_instructions_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            (target / "AGENTS.md").unlink()

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertIn("required_file", self.issue_categories(results))

    def test_unpersonalized_project_name_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            config = self.load_config(target)
            config["project"]["name"] = "evidence-wiki"
            self.write_config(target, config)

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertIn("project_identity", self.issue_categories(results))

    def test_template_log_examples_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            log_path = target / "log.md"
            log_path.write_text(log_path.read_text() + "\n- Template initialized\n")

            results = SMOKE.run_checks(target)

            self.assertFalse(results["ok"])
            self.assertIn("log", self.issue_categories(results))

    def test_json_cli_output_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))

            exit_code, output = self.run_smoke_cli(target, "json")
            payload = json.loads(output)

            self.assertEqual(0, exit_code)
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["issues"])
            self.assertEqual(0, payload["summary"]["issue_count"])

    def test_text_cli_returns_nonzero_for_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            (target / "index.md").write_text("# Missing Project Name\n")

            exit_code, output = self.run_smoke_cli(target)

            self.assertEqual(1, exit_code)
            self.assertIn("Smoke validation failed.", output)
            self.assertIn("index.md", output)


#: The exact sentences a workspace authorizing an id nothing supplies produced before
#: provider registration existed.  They are written out rather than rebuilt from the
#: module's constants because that is the property under test: hosts parse these, and a
#: test that recomputes the string from the same source could not catch it changing.
BUILTIN_ACQUISITION_UNKNOWN_MESSAGE = (
    "research.yml integrations.acquisition.providers has unknown provider(s): not-a-real-provider. "
    "Allowed providers: arxiv, openalex, github, web"
)
BUILTIN_ACQUISITION_DUPLICATE_MESSAGE = (
    "research.yml integrations.acquisition.providers has duplicate provider(s): arxiv"
)
BUILTIN_ACQUISITION_RECOMMENDATION = "Use only supported acquisition providers: arxiv, openalex, github, web."
BUILTIN_DISCOVERY_UNKNOWN_MESSAGE = (
    "research.yml integrations.discovery.providers has unknown provider(s): not-a-real-provider. "
    "Allowed providers: arxiv, openalex, github, search, standards, standards:iso-open-data, "
    "standards:eu-product-requirements, standards:uk-geospatial-register, standards:nist, "
    "legal, authors, companions"
)
BUILTIN_DISCOVERY_RECOMMENDATION = (
    "Use only supported discovery providers: arxiv, openalex, github, search, standards, "
    "standards:iso-open-data, standards:eu-product-requirements, standards:uk-geospatial-register, "
    "standards:nist."
)


class SmokeValidateRegisteredProviderTests(unittest.TestCase):
    """Smoke must resolve registered provider ids, and refuse what it cannot resolve.

    Smoke is the "safe to operate *here*" gate: it is the one check in the package that
    is allowed to depend on the environment, which is exactly what a pip-installed
    provider is.  Once ``research.yml`` may authorize an id no built-in tuple contains,
    two things have to be true at once.  A workspace authorizing a provider whose
    distribution is installed must pass, because otherwise registration is unusable.  And
    a workspace authorizing one the environment cannot supply must *fail*, because an
    authorization nothing can satisfy is deploy drift on an authorization boundary — the
    place this package never shrugs — and because every other provider-list defect here
    is already fatal.  Softening exactly the third-party case would make registered
    providers less honest than built-ins, inverting the change request's intent.

    The third property is the one that costs the most to keep: with nothing installed,
    every sentence is byte-for-byte what it was before registration existed.
    """

    maxDiff = None

    def setUp(self):
        refresh_provider_plugin_caches()

    def tearDown(self):
        refresh_provider_plugin_caches()

    def create_workspace(self, root: Path) -> Path:
        target = root / "workspace"
        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = INIT.main(
                [
                    "--target",
                    str(target),
                    "--project-name",
                    "registered-provider-workspace",
                    "--project-description",
                    "Workspace created for registered provider smoke tests.",
                ]
            )
        self.assertEqual(0, exit_code)
        (target / "sources" / "discovery").mkdir(exist_ok=True)
        return target

    def authorize(self, target: Path, *, acquisition=None, discovery=None) -> None:
        """Rewrite both integration blocks, so one call never inherits the previous one."""
        config = yaml.safe_load((target / "research.yml").read_text())
        config["integrations"]["acquisition"] = {
            "enabled": acquisition is not None,
            "providers": list(acquisition or []),
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        config["integrations"]["discovery"] = {
            "enabled": discovery is not None,
            "providers": list(discovery or []),
            "candidate_store_path": "sources/discovery/candidates.jsonl",
        }
        (target / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    def provider_issues(self, results: dict) -> list[dict]:
        return [
            item
            for item in results["issues"]
            if str(item.get("field", "")).endswith(".providers")
        ]

    def only_provider_issue(self, results: dict) -> dict:
        found = self.provider_issues(results)
        self.assertEqual(1, len(found), results["issues"])
        return found[0]

    def test_installed_registered_provider_is_authorized_like_a_builtin(self):
        """A distribution that is actually installed makes its id a passing authorization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(
                target,
                acquisition=[ACQUISITION_PROVIDER_ID],
                discovery=[DISCOVERY_PROVIDER_ID],
            )

            with installed_provider_plugins():
                results = SMOKE.run_checks(target)

            self.assertEqual([], results["issues"], results["issues"])
            self.assertTrue(results["ok"])

    def test_authorized_but_uninstalled_provider_fails_smoke_with_the_registration_code(self):
        """Not installed is fatal, not advisory, and says which entry-point group to install into."""
        for phase, provider_id, group in (
            ("acquisition", ACQUISITION_PROVIDER_ID, "evidence_wiki.acquisition_providers"),
            ("discovery", DISCOVERY_PROVIDER_ID, "evidence_wiki.discovery_providers"),
        ):
            with self.subTest(phase=phase):
                with tempfile.TemporaryDirectory() as tmpdir:
                    target = self.create_workspace(Path(tmpdir))
                    self.authorize(target, **{phase: [provider_id]})

                    results = SMOKE.run_checks(target)

                    finding = self.only_provider_issue(results)
                    self.assertEqual("HIGH", finding["severity"])
                    self.assertEqual("config_shape", finding["category"])
                    self.assertEqual("PROVIDER_NOT_REGISTERED", finding["error_code"])
                    self.assertIn(f"Install a distribution that registers '{provider_id}'", finding["recommendation"])
                    self.assertIn(group, finding["recommendation"])
                    self.assertIn("authorization boundary", finding["recommendation"])
                    self.assertFalse(results["ok"])

    def test_installed_but_invalid_registration_says_repair_not_install(self):
        """Telling an operator to install what is already installed is the failure this split prevents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            # The broken fixture declares this id; authorizing it is what an operator
            # reading the distribution's documentation would write.
            self.authorize(target, acquisition=["Keepa_Broken_Fixture"])

            with installed_provider_plugins("invalid-declaration", base=False):
                results = SMOKE.run_checks(target)

            finding = self.only_provider_issue(results)
            self.assertEqual("PROVIDER_REGISTRATION_INVALID", finding["error_code"])
            self.assertIn("Upgrade or fix keepa-broken-fixture", finding["recommendation"])
            self.assertNotIn("Install a distribution", finding["recommendation"])
            self.assertFalse(results["ok"])

    def test_a_duplicated_id_names_both_distributions_rather_than_picking_one(self):
        """Two distributions claiming one id refuses both, so the report must name both."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(target, acquisition=[ACQUISITION_PROVIDER_ID])

            with installed_provider_plugins("duplicate-id"):
                results = SMOKE.run_checks(target)

            finding = self.only_provider_issue(results)
            self.assertEqual("PROVIDER_REGISTRATION_INVALID", finding["error_code"])
            self.assertIn("keepa-fixture", finding["recommendation"])
            self.assertIn("keepa-rival-fixture", finding["recommendation"])
            self.assertFalse(results["ok"])

    def test_builtin_providers_are_unaffected_by_installed_registrations(self):
        """An unrelated installed plugin must not change a built-in-only workspace at all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(target, acquisition=["arxiv"], discovery=["openalex"])

            without = SMOKE.run_checks(target)
            with installed_provider_plugins():
                with_plugins = SMOKE.run_checks(target)

            self.assertEqual([], without["issues"])
            self.assertEqual(without["issues"], with_plugins["issues"])
            self.assertTrue(without["ok"])
            self.assertTrue(with_plugins["ok"])

    def test_no_plugins_installed_keeps_every_provider_sentence_byte_identical(self):
        """The backward-compatibility criterion: no entry points, no message change.

        Hosts parse these strings, so they are asserted with ``assertEqual`` against
        literals rather than against anything recomputed from the module's constants.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))

            self.authorize(target, acquisition=["not-a-real-provider"])
            unknown = self.only_provider_issue(SMOKE.run_checks(target))
            self.assertEqual(BUILTIN_ACQUISITION_UNKNOWN_MESSAGE, unknown["message"])

            self.authorize(target, acquisition=["arxiv", "arxiv"])
            duplicate = self.only_provider_issue(SMOKE.run_checks(target))
            self.assertEqual(BUILTIN_ACQUISITION_DUPLICATE_MESSAGE, duplicate["message"])
            # A shape defect is not a registration question, so it keeps the old advice
            # and gains no code.
            self.assertEqual(BUILTIN_ACQUISITION_RECOMMENDATION, duplicate["recommendation"])
            self.assertNotIn("error_code", duplicate)

            self.authorize(target, acquisition=[])
            empty = self.only_provider_issue(SMOKE.run_checks(target))
            self.assertEqual("enabled acquisition must list at least one provider", empty["message"])
            self.assertNotIn("error_code", empty)

            self.authorize(target, discovery=["not-a-real-provider"])
            discovery_unknown = self.only_provider_issue(SMOKE.run_checks(target))
            self.assertEqual(BUILTIN_DISCOVERY_UNKNOWN_MESSAGE, discovery_unknown["message"])

            self.authorize(target, discovery=["legal"])
            legacy = [item for item in SMOKE.run_checks(target)["issues"] if item["category"] == "deprecated_config"]
            self.assertEqual(1, len(legacy))
            self.assertEqual(
                "integrations.discovery.providers contains legacy strategy id 'legal'",
                legacy[0]["message"],
            )

    def test_the_unresolved_id_answer_does_not_depend_on_which_environment_asks(self):
        """A typo and a missing distribution are one observation, answered one way.

        Registration must never make the same ``research.yml`` produce a different code
        in two virtual environments; that environment-dependence is the defect that kept
        registration checks out of lint entirely.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(target, acquisition=["not-a-real-provider"])

            bare = self.only_provider_issue(SMOKE.run_checks(target))
            with installed_provider_plugins():
                with_unrelated_plugin = self.only_provider_issue(SMOKE.run_checks(target))

            self.assertEqual("PROVIDER_NOT_REGISTERED", bare["error_code"])
            self.assertEqual(bare["error_code"], with_unrelated_plugin["error_code"])
            self.assertEqual(bare["recommendation"], with_unrelated_plugin["recommendation"])

    def test_a_clean_workspace_renders_byte_identically(self):
        """The additive issue field must be invisible where there are no issues."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))

            results = SMOKE.run_checks(target)

            self.assertEqual("Smoke validation passed.\n", SMOKE.render_text(results))
            self.assertEqual([], results["issues"])

    def test_the_registration_failure_reaches_the_workspace_unsafe_refusal(self):
        """Prove the gate is connected end to end rather than assuming it.

        The chain is: a HIGH smoke issue clears ``results['ok']``, which
        ``workspace_status.smoke_section`` reports, which ``readiness_section`` turns
        into the ``attention_required`` verdict, which ``verify_runtime_guards``
        refuses with ``ORCHESTRATION_WORKSPACE_UNSAFE``.  Each hop is exercised here so
        that a future change breaking any one of them fails on this test rather than
        silently downgrading an authorization defect to a report nobody blocks on.
        """
        status = load_script_module("registered_provider_workspace_status", STATUS_SCRIPT_PATH)
        controller = load_script_module("registered_provider_controller", CONTROLLER_SCRIPT_PATH)
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(target, acquisition=[ACQUISITION_PROVIDER_ID])

            document = status.build_status_document(target)

            readiness = document["readiness"]
            self.assertFalse(document["smoke"]["ok"])
            self.assertEqual("attention_required", readiness["verdict"])
            self.assertTrue(
                any("Smoke validation failed" in reason for reason in readiness["reasons"]),
                readiness["reasons"],
            )

            with self.assertRaises(controller.OrchestrationControllerError) as caught:
                controller.verify_runtime_guards(target, {})
            self.assertEqual("ORCHESTRATION_WORKSPACE_UNSAFE", caught.exception.error_code)
            self.assertEqual("attention_required", caught.exception.details["readiness_verdict"])

    def test_an_installed_registration_leaves_the_workspace_operable(self):
        """The same workspace, with the distribution present, must not be gated at all."""
        controller = load_script_module("registered_provider_controller", CONTROLLER_SCRIPT_PATH)
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.create_workspace(Path(tmpdir))
            self.authorize(target, acquisition=[ACQUISITION_PROVIDER_ID])

            with installed_provider_plugins():
                controller.verify_runtime_guards(target, {})  # must not raise


if __name__ == "__main__":
    unittest.main()
