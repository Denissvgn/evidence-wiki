import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests._provider_plugin_fixture import (
    ACQUISITION_PROVIDER_ID,
    DISCOVERY_PROVIDER_ID,
    installed_provider_plugins,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCTOR_PATH = REPO_ROOT / "workspace-template" / "scripts" / "doctor.py"


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir()
    for relative in ("raw", "sources", "wiki", "scripts", "docs", "skills"):
        (workspace / relative).mkdir()
    (workspace / "research.yml").write_text(
        "project: {}\n"
        "raw: {}\n"
        "sources: {}\n"
        "wiki: {}\n"
        "taxonomy: {}\n"
        "ingest: {}\n"
        "lint: {}\n"
        "outputs: {}\n"
        "integrations: {}\n",
        encoding="utf-8",
    )
    for relative in ("AGENTS.md", "index.md", "log.md"):
        (workspace / relative).write_text(f"# {relative}\n", encoding="utf-8")
    (workspace / "workspace-system.yml").write_text(
        "workspace_system:\n"
        "  starter_version: \"0.5.5\"\n"
        "  schema_version: \"0.1\"\n"
        "  compatible_research_yml_contract: \"0.1\"\n"
    )
    return workspace


class FakeEnvironment:
    def __init__(
        self,
        *,
        python_version=(3, 11, 0),
        yaml_error: Exception | None = None,
        pypdf_error: Exception | None = None,
    ):
        self.python_version = python_version
        self.yaml_error = yaml_error
        self.pypdf_error = pypdf_error

    def import_yaml(self):
        if self.yaml_error is not None:
            raise self.yaml_error
        import yaml

        return yaml

    def import_pypdf(self):
        if self.pypdf_error is not None:
            raise self.pypdf_error
        return mock.Mock(__version__="6.14.0")

    def which(self, name: str) -> str | None:
        return f"/usr/bin/{name}"

    def command_version(self, command: list[str]) -> str | None:
        return f"{command[0]} version fixture"

    def write_probe(self, directory: Path) -> tuple[bool, str | None]:
        return True, None

    def now_utc(self) -> str:
        return "2026-06-13T00:00:00Z"


class DoctorScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doctor = load_script_module("evidence_wiki_doctor_tests", DOCTOR_PATH)

    def test_json_report_contains_contract_and_writable_workspace_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))

            report = self.doctor.build_report(workspace, env=FakeEnvironment())

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual("ok", report["verdict"])
        self.assertEqual("2026-06-13T00:00:00Z", report["generated_at"])
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("ok", checks["python"]["status"])
        self.assertEqual("ok", checks["pyyaml"]["status"])
        self.assertEqual("ok", checks["pypdf"]["status"])
        self.assertTrue(checks["pypdf"]["required"])
        self.assertEqual("ok", checks["pdftotext"]["status"])
        self.assertEqual("ok", checks["git"]["status"])
        self.assertEqual("ok", checks["workspace_write"]["status"])
        self.assertEqual("ok", checks["contract"]["status"])
        self.assertEqual("ok", checks["semantic_retrieval"]["status"])
        self.assertEqual("ok", checks["secret_exposure"]["status"])
        self.assertEqual("0.5.5", checks["contract"]["details"]["starter_version"])
        self.assertEqual("0.1", checks["contract"]["details"]["schema_version"])
        self.assertEqual("0.1", checks["contract"]["details"]["compatible_research_yml_contract"])
        self.assertEqual(
            ["docs", "raw", "root", "scripts", "sources", "wiki"],
            sorted(checks["workspace_write"]["details"]["checked"]),
        )

    def test_missing_optional_tools_degrade_with_path_manipulation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            env = FakeEnvironment()
            with mock.patch.object(env, "which", return_value=None):
                report = self.doctor.build_report(workspace, env=env)

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("degraded", report["verdict"])
        self.assertEqual("ok", checks["pdftotext"]["status"])
        self.assertFalse(checks["pdftotext"]["required"])
        self.assertFalse(checks["pdftotext"]["details"]["available"])
        self.assertIn("pypdf backend remains available", checks["pdftotext"]["implication"])
        self.assertEqual("missing", checks["git"]["status"])
        self.assertFalse(checks["git"]["required"])
        self.assertIn("version-control", checks["git"]["implication"])

    def test_workspace_health_dependency_override_is_partial_and_poppler_is_informational(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            health_module = sys.modules["_workspace_health"]
            with mock.patch.object(health_module.importlib.util, "find_spec", return_value=object()):
                health = health_module.evaluate_workspace_health(
                    workspace,
                    optional_tool_availability={"pdftotext": False},
                )

        self.assertEqual("healthy", health["status"])
        self.assertNotIn("REQUIRED_DEPENDENCY_MISSING", health["finding_codes"])
        self.assertNotIn("OPTIONAL_TOOL_MISSING", health["finding_codes"])

    def test_workspace_health_treats_broken_pypdf_spec_lookup_as_missing(self):
        health_module = sys.modules["_workspace_health"]
        for error in (ImportError("import system unavailable"), ValueError("invalid module spec")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmpdir:
                workspace = make_workspace(Path(tmpdir))
                with mock.patch.object(health_module.importlib.util, "find_spec", side_effect=error):
                    health = health_module.evaluate_workspace_health(
                        workspace,
                        optional_tool_availability={"pdftotext": False},
                    )

            self.assertEqual("invalid", health["status"])
            self.assertIn("REQUIRED_DEPENDENCY_MISSING", health["finding_codes"])

    def test_missing_poppler_alone_does_not_degrade_portable_pdf_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            env = FakeEnvironment()

            def which(name: str) -> str | None:
                return None if name == "pdftotext" else f"/usr/bin/{name}"

            with mock.patch.object(env, "which", side_effect=which):
                report = self.doctor.build_report(workspace, env=env)

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("ok", report["verdict"])
        self.assertEqual("ok", checks["pdftotext"]["status"])
        self.assertFalse(checks["pdftotext"]["details"]["available"])

    def test_missing_configured_poppler_is_a_required_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            research_path = workspace / "research.yml"
            research_path.write_text(
                research_path.read_text(encoding="utf-8").replace(
                    "sources: {}\n",
                    "sources:\n  pdf_extractor: poppler\n",
                ),
                encoding="utf-8",
            )
            env = FakeEnvironment()

            def which(name: str) -> str | None:
                return None if name == "pdftotext" else f"/usr/bin/{name}"

            with mock.patch.object(env, "which", side_effect=which):
                report = self.doctor.build_report(workspace, env=env)

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("missing", report["verdict"])
        self.assertEqual("missing", checks["pdftotext"]["status"])
        self.assertTrue(checks["pdftotext"]["required"])
        self.assertFalse(checks["pdftotext"]["details"]["available"])
        self.assertIn("sources.pdf_extractor to pypdf", checks["pdftotext"]["remediation"])
        self.assertEqual("invalid", report["workspace_health"]["status"])
        self.assertIn("REQUIRED_DEPENDENCY_MISSING", report["workspace_health"]["finding_codes"])

    def test_semantic_retrieval_check_reports_enabled_command_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            (workspace / "research.yml").write_text(
                "integrations:\n"
                "  retrieval:\n"
                "    semantic:\n"
                "      enabled: true\n"
                "      provider: local-semantic\n"
                "      transport: command\n"
                "      command:\n"
                "        - semantic-search\n",
                encoding="utf-8",
            )

            report = self.doctor.build_report(workspace, env=FakeEnvironment())

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("ok", checks["semantic_retrieval"]["status"])
        self.assertEqual("local-semantic", checks["semantic_retrieval"]["details"]["provider"])

    def test_readable_env_file_warns_without_printing_secret_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            (workspace / ".env").write_text("OPENALEX_API_KEY=super-secret-value\n", encoding="utf-8")

            report = self.doctor.build_report(workspace, env=FakeEnvironment())

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("degraded", report["verdict"])
        self.assertEqual("degraded", checks["secret_exposure"]["status"])
        serialized = json.dumps(checks["secret_exposure"], sort_keys=True)
        self.assertIn(".env", serialized)
        self.assertNotIn("super-secret-value", serialized)

    def test_missing_pyyaml_is_required_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            stdout = io.StringIO()
            env = FakeEnvironment(yaml_error=ImportError("No module named yaml"))

            with contextlib.redirect_stdout(stdout):
                exit_code = self.doctor.main(["--project-root", str(workspace), "--format", "json"], env=env)

        report = json.loads(stdout.getvalue())
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(1, exit_code)
        self.assertEqual("missing", report["verdict"])
        self.assertEqual("missing", checks["pyyaml"]["status"])
        self.assertTrue(checks["pyyaml"]["required"])

    def test_missing_pypdf_is_required_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            stdout = io.StringIO()
            env = FakeEnvironment(pypdf_error=ImportError("No module named pypdf"))

            with contextlib.redirect_stdout(stdout):
                exit_code = self.doctor.main(["--project-root", str(workspace), "--format", "json"], env=env)

        report = json.loads(stdout.getvalue())
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(1, exit_code)
        self.assertEqual("missing", report["verdict"])
        self.assertEqual("missing", checks["pypdf"]["status"])
        self.assertTrue(checks["pypdf"]["required"])
        self.assertIn("pypdf", checks["pypdf"]["remediation"])

    def test_python_too_old_is_required_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            stdout = io.StringIO()
            env = FakeEnvironment(python_version=(3, 9, 18))

            with contextlib.redirect_stdout(stdout):
                exit_code = self.doctor.main(["--project-root", str(workspace), "--format", "json"], env=env)

        report = json.loads(stdout.getvalue())
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(1, exit_code)
        self.assertEqual("missing", report["verdict"])
        self.assertEqual("missing", checks["python"]["status"])
        self.assertTrue(checks["python"]["required"])


class NormalizerAdapterDoctorTests(unittest.TestCase):
    """Doctor is where an auditor asks what a workspace may execute before it does.

    A configured adapter is the one place normalization runs something the package did
    not ship, so the declaration has to be visible without opening research.yml — and
    doctor must only report it, never run it.
    """

    def setUp(self):
        self.doctor = load_script_module("evidence_wiki_doctor_adapters", DOCTOR_PATH)

    def report(self, workspace: Path) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.doctor.main(["--project-root", str(workspace), "--format", "json"], env=FakeEnvironment())
        return json.loads(stdout.getvalue())

    def check(self, workspace: Path) -> dict:
        return {check["id"]: check for check in self.report(workspace)["checks"]}["normalization_adapters"]

    def with_normalization(self, workspace: Path, section: str) -> Path:
        config = workspace / "research.yml"
        config.write_text(config.read_text(encoding="utf-8") + section, encoding="utf-8")
        return workspace

    ADAPTER_SECTION = (
        "normalization:\n"
        "  adapters:\n"
        "    - kinds: [structured_data]\n"
        "      provider: command\n"
        '      command: ["autoseller-normalize", "--format", "json"]\n'
        "      name: autoseller-normalize\n"
        '      version: "1.4.0"\n'
        "      timeout_seconds: 90\n"
    )

    def test_a_workspace_with_no_adapters_says_it_runs_nothing_external(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            check = self.check(make_workspace(Path(tmpdir)))

        self.assertEqual("ok", check["status"])
        self.assertFalse(check["required"])
        self.assertEqual(0, check["details"]["configured"])
        self.assertEqual([], check["details"]["adapters"])
        self.assertIn("no external commands", check["implication"])

    def test_a_configured_adapter_is_listed_in_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.with_normalization(make_workspace(Path(tmpdir)), self.ADAPTER_SECTION)
            check = self.check(workspace)

        self.assertEqual("ok", check["status"], check["message"])
        self.assertEqual(1, check["details"]["configured"])
        self.assertEqual(
            {
                "kinds": ["structured_data"],
                "provider": "command",
                "command": ["autoseller-normalize", "--format", "json"],
                "name": "autoseller-normalize",
                "version": "1.4.0",
                "timeout_seconds": 90,
            },
            check["details"]["adapters"][0],
        )
        self.assertIn("structured_data", check["message"])
        self.assertIn("executes these commands", check["implication"])

    def test_a_configured_adapter_does_not_degrade_the_verdict(self):
        # Authorizing an adapter is a legitimate configuration, not a defect; the audit
        # value is in the listing, not in a warning.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.with_normalization(make_workspace(Path(tmpdir)), self.ADAPTER_SECTION)
            report = self.report(workspace)

        self.assertEqual("ok", report["verdict"], report["checks"])

    def test_an_invalid_section_is_reported_as_degraded_with_its_code(self):
        broken = self.ADAPTER_SECTION.replace(
            '      command: ["autoseller-normalize", "--format", "json"]\n',
            '      command: "autoseller-normalize --format json"\n',
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.with_normalization(make_workspace(Path(tmpdir)), broken)
            check = self.check(workspace)

        self.assertEqual("degraded", check["status"])
        self.assertEqual("CONFIG_INVALID", check["details"]["error_code"])
        self.assertIn("must be a list of arguments", check["message"])
        self.assertIn("docs/research-yml.md", check["remediation"])

    def test_doctor_never_executes_the_configured_command(self):
        # The command names a program that would fail loudly if run.
        section = self.ADAPTER_SECTION.replace(
            '      command: ["autoseller-normalize", "--format", "json"]\n',
            '      command: ["/definitely/not/a/real/binary-ew"]\n',
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.with_normalization(make_workspace(Path(tmpdir)), section)
            with mock.patch("subprocess.run", side_effect=AssertionError("doctor ran the adapter")):
                check = self.check(workspace)

        self.assertEqual("ok", check["status"])
        self.assertEqual(["/definitely/not/a/real/binary-ew"], check["details"]["adapters"][0]["command"])


class RegisteredProviderDoctorTests(unittest.TestCase):
    """Doctor is the only place an auditor can see what a workspace *could* reach.

    Registration is packaging metadata, so installing a distribution makes a provider id
    available and nothing more; ``research.yml`` is still what enables it. Those two
    states have four combinations with four different fixes, and telling them apart is
    the entire job of this section — which is why every test below is written as a
    question an operator would actually ask of a workspace that "isn't working".

    The section must also never make doctor worse at its other job: with nothing
    installed, everything outside the section stays exactly as it was, and a broken
    plugin is a finding rather than a crashed report.
    """

    #: The whole ordered contract of doctor's checks. Pinned because a host reads them
    #: positionally in text output, and because a check that silently disappeared would
    #: otherwise be caught by nothing.
    EXPECTED_CHECK_IDS = [
        "python",
        "pyyaml",
        "pypdf",
        "pdftotext",
        "git",
        "workspace_write",
        "contract",
        "semantic_retrieval",
        "normalization_adapters",
        "acquisition_mode",
        "registered_providers",
        "secret_exposure",
        "workspace_health",
    ]

    CREDENTIAL_NAME = "KEEPA_FIXTURE_API_KEY"
    CREDENTIAL_VALUE = "super-secret-value-never-printed"

    def setUp(self):
        self.doctor = load_script_module("evidence_wiki_doctor_registered_providers", DOCTOR_PATH)

    def workspace(
        self,
        tmpdir: str,
        *,
        acquisition: tuple[str, ...] = (),
        discovery: tuple[str, ...] = (),
        acquisition_enabled: bool = True,
        discovery_enabled: bool = False,
        env_file: str | None = None,
    ) -> Path:
        workspace = make_workspace(Path(tmpdir))
        (workspace / "research.yml").write_text(
            "project: {}\n"
            "raw: {}\n"
            "sources: {}\n"
            "wiki: {}\n"
            "taxonomy: {}\n"
            "ingest: {}\n"
            "lint: {}\n"
            "outputs: {}\n"
            "integrations:\n"
            "  acquisition:\n"
            f"    enabled: {'true' if acquisition_enabled else 'false'}\n"
            "    providers: [" + ", ".join(acquisition) + "]\n"
            "  discovery:\n"
            f"    enabled: {'true' if discovery_enabled else 'false'}\n"
            "    providers: [" + ", ".join(discovery) + "]\n",
            encoding="utf-8",
        )
        if env_file is not None:
            (workspace / ".env").write_text(env_file, encoding="utf-8")
        return workspace

    def report(self, workspace: Path) -> dict:
        return self.doctor.build_report(workspace, env=FakeEnvironment())

    def section(self, workspace: Path) -> dict:
        checks = {check["id"]: check for check in self.report(workspace)["checks"]}
        return checks["registered_providers"]

    def test_the_ordered_check_contract_includes_the_new_section_and_drops_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.report(self.workspace(tmpdir))

        self.assertEqual(self.EXPECTED_CHECK_IDS, [check["id"] for check in report["checks"]])

    def test_an_environment_with_no_registrations_reports_the_section_as_present_and_empty(self):
        # Present-but-empty rather than absent, deliberately: an absent section cannot be
        # told apart from a doctor that does not know about registration at all, and
        # "nothing is installed" is a positive answer an auditor needs to be able to read.
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.report(self.workspace(tmpdir))

        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        self.assertEqual("ok", section["status"])
        self.assertFalse(section["required"])
        self.assertEqual("ok", report["verdict"])
        self.assertEqual("No third-party providers are registered in this environment.", section["message"])
        self.assertEqual([], section["details"]["registered"])
        self.assertEqual([], section["details"]["invalid"])
        self.assertEqual([], section["details"]["findings"])
        self.assertEqual(
            {"registered": 0, "enabled": 0, "available": 0, "invalid": 0},
            section["details"]["counts"],
        )

    def test_with_nothing_installed_the_secrets_check_keeps_its_pre_registration_output(self):
        # The one existing check this unit touches. With no declared credential names it
        # must be byte-identical to what it emitted before registration existed, so the
        # extension cannot become drift for every workspace that has no plugins.
        cases = (
            (None, {"readable_env_files": []}, "No readable .env file found at the workspace or invocation root."),
            (
                "OPENALEX_API_KEY=value\n",
                {"readable_env_files": [".env"]},
                "Readable .env file(s) are present; values were not inspected or printed.",
            ),
        )
        for env_file, expected_details, expected_message in cases:
            with self.subTest(env_file=bool(env_file)), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.workspace(tmpdir, env_file=env_file)
                checks = {check["id"]: check for check in self.report(workspace)["checks"]}
                secrets = checks["secret_exposure"]

            self.assertEqual(expected_details, secrets["details"])
            self.assertEqual(expected_message, secrets["message"])
            if env_file is None:
                self.assertEqual(
                    "Operator-managed per-run environment injection remains the expected secret path.",
                    secrets["implication"],
                )
                self.assertEqual("No action required.", secrets["remediation"])
            else:
                self.assertEqual(
                    "Provider credentials can leak into source/workspace state if .env files are treated "
                    "as runtime configuration.",
                    secrets["implication"],
                )
                self.assertEqual(
                    "Move provider keys into the operator secret store, rotate exposed keys, and keep "
                    "repo-root .env development-only.",
                    secrets["remediation"],
                )

    def test_installing_a_provider_changes_only_the_registration_and_secrets_checks(self):
        # Pins that nothing else in doctor became environment-dependent. Every other
        # check is a function of the workspace tree and must not notice a pip install.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir)
            before = self.report(workspace)
            with installed_provider_plugins():
                after = self.report(workspace)

        changed = {
            check["id"]
            for check, other in zip(before["checks"], after["checks"], strict=True)
            if json.dumps(check, sort_keys=True) != json.dumps(other, sort_keys=True)
        }
        secrets = {check["id"]: check for check in after["checks"]}["secret_exposure"]
        self.assertEqual({"registered_providers", "secret_exposure"}, changed)
        self.assertEqual(before["verdict"], after["verdict"])
        # And the secrets check changed only by learning the declared *names*: its status,
        # message, and the .env listing it already produced are untouched.
        for field in ("status", "message", "implication", "remediation"):
            with self.subTest(field=field):
                self.assertEqual(
                    {check["id"]: check for check in before["checks"]}["secret_exposure"][field],
                    secrets[field],
                )
        self.assertEqual([self.CREDENTIAL_NAME], secrets["details"]["declared_credentials"])
        self.assertEqual([], secrets["details"]["exposed_credentials"])

    def test_a_registration_research_yml_does_not_authorize_is_available_not_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir)
            with installed_provider_plugins():
                report = self.report(workspace)

        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        entries = {entry["id"]: entry for entry in section["details"]["registered"]}
        self.assertEqual("ok", section["status"])
        self.assertEqual("ok", report["verdict"])
        self.assertEqual([], section["details"]["findings"])
        self.assertEqual(
            {ACQUISITION_PROVIDER_ID, DISCOVERY_PROVIDER_ID},
            set(entries),
        )
        for provider_id, entry in entries.items():
            with self.subTest(provider=provider_id):
                self.assertEqual("available", entry["state"])
                self.assertFalse(entry["authorized"])
        self.assertEqual(
            {"registered": 2, "enabled": 0, "available": 2, "invalid": 0},
            section["details"]["counts"],
        )

    def test_an_authorized_registration_is_listed_as_enabled_with_its_declaration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir, acquisition=(ACQUISITION_PROVIDER_ID,))
            with installed_provider_plugins():
                report = self.report(workspace)

        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        entries = {entry["id"]: entry for entry in section["details"]["registered"]}
        acquisition = entries[ACQUISITION_PROVIDER_ID]
        self.assertEqual("ok", section["status"])
        self.assertEqual("ok", report["verdict"])
        self.assertEqual("enabled", acquisition["state"])
        self.assertTrue(acquisition["authorized"])
        self.assertEqual("acquisition", acquisition["phase"])
        self.assertEqual("keepa-fixture", acquisition["distribution"])
        self.assertEqual("0.1.0", acquisition["version"])
        self.assertEqual(1, acquisition["provider_api_version"])
        self.assertEqual("evidence_wiki.acquisition_providers", acquisition["entry_point_group"])
        self.assertEqual(
            ["api.keepa-fixture.invalid", "assets.keepa-fixture.invalid"],
            acquisition["capabilities"]["allowed_domains"],
        )
        self.assertEqual({"requests": 60, "per": "minute"}, acquisition["capabilities"]["rate_limit"])
        self.assertEqual([self.CREDENTIAL_NAME], acquisition["capabilities"]["credentials"])
        self.assertEqual(["market-data/price_history"], acquisition["capabilities"]["request_kinds"])
        # The other registration is installed too and stays *available*: authorizing one
        # provider must never quietly enable the rest of its distribution.
        self.assertEqual("available", entries[DISCOVERY_PROVIDER_ID]["state"])

    def test_an_authorized_provider_that_is_not_installed_is_an_error_the_operator_can_act_on(self):
        # The §2.7 posture: smoke already refuses this workspace. Doctor is where the
        # operator learns which id, in which entry-point group, and what to install.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir, acquisition=(ACQUISITION_PROVIDER_ID,))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = self.doctor.main(
                    ["--project-root", str(workspace), "--format", "json"], env=FakeEnvironment()
                )

        report = json.loads(stdout.getvalue())
        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        finding = section["details"]["findings"][0]
        self.assertEqual(1, exit_code)
        self.assertEqual("missing", report["verdict"])
        self.assertEqual("missing", section["status"])
        self.assertTrue(section["required"])
        self.assertIn(ACQUISITION_PROVIDER_ID, section["message"])
        self.assertEqual("error", finding["severity"])
        self.assertEqual("PROVIDER_NOT_REGISTERED", finding["code"])
        self.assertEqual(ACQUISITION_PROVIDER_ID, finding["provider_id"])
        self.assertEqual("evidence_wiki.acquisition_providers", finding["details"]["entry_point_group"])
        self.assertIn("evidence_wiki.acquisition_providers", finding["remediation"])

    def test_a_built_in_provider_id_is_never_reported_as_unregistered(self):
        # The closed built-in list is still the universe when nothing is installed; a
        # workspace authorizing only built-ins must not gain a finding from this section.
        with tempfile.TemporaryDirectory() as tmpdir:
            section = self.section(
                self.workspace(tmpdir, acquisition=("arxiv", "web"), discovery=("openalex", "standards:nist"))
            )

        self.assertEqual("ok", section["status"])
        self.assertEqual([], section["details"]["findings"])

    def test_a_duplicate_id_collision_is_an_error_naming_both_distributions(self):
        # The loader refuses both claims so behaviour cannot depend on install order.
        # From outside, the id simply is not there — unless doctor names who claimed it.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir)
            with installed_provider_plugins("duplicate-id"):
                section = self.section(workspace)

        finding = next(
            item for item in section["details"]["findings"] if item["provider_id"] == ACQUISITION_PROVIDER_ID
        )
        self.assertEqual("degraded", section["status"])
        self.assertFalse(section["required"])
        self.assertEqual("error", finding["severity"])
        self.assertEqual("PROVIDER_REGISTRATION_INVALID", finding["code"])
        self.assertEqual(["keepa-fixture", "keepa-rival-fixture"], finding["details"]["distributions"])
        for distribution in ("keepa-fixture", "keepa-rival-fixture"):
            with self.subTest(distribution=distribution):
                self.assertIn(distribution, finding["message"])
        self.assertNotIn(
            ACQUISITION_PROVIDER_ID,
            [entry["id"] for entry in section["details"]["registered"]],
        )

    def test_an_authorized_duplicate_id_collision_still_names_both_distributions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir, acquisition=(ACQUISITION_PROVIDER_ID,))
            with installed_provider_plugins("duplicate-id"):
                section = self.section(workspace)

        finding = section["details"]["findings"][0]
        self.assertEqual("missing", section["status"])
        self.assertTrue(section["required"])
        self.assertEqual("error", finding["severity"])
        # Installed-but-invalid, not "go install it": telling an operator to install what
        # is already installed is the failure mode the two codes exist to keep apart.
        self.assertEqual("PROVIDER_REGISTRATION_INVALID", finding["code"])
        for distribution in ("keepa-fixture", "keepa-rival-fixture"):
            with self.subTest(distribution=distribution):
                self.assertIn(distribution, finding["message"])

    def test_a_registration_that_cannot_load_or_validate_is_listed_with_its_reason(self):
        cases = (
            ("invalid-declaration", "keepa-broken-fixture"),
            ("import-error", "keepa-exploding-fixture"),
            ("reserved-id", "keepa-reserved-fixture"),
        )
        for variant, distribution in cases:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmpdir:
                workspace = self.workspace(tmpdir)
                with installed_provider_plugins(variant, base=False):
                    section = self.section(workspace)

            entries = [entry for entry in section["details"]["invalid"] if entry["distribution"] == distribution]
            self.assertTrue(entries, section["details"]["invalid"])
            self.assertEqual("degraded", section["status"])
            self.assertFalse(section["required"])
            for entry in entries:
                self.assertTrue(entry["reason"], entry)
                self.assertIn(entry["phase"], ("acquisition", "discovery"))
            # A registration that never loads must be visible, not silently absent: an
            # invisible broken plugin and an uninstalled one are the same picture.
            warnings = [
                item
                for item in section["details"]["findings"]
                if item["severity"] == "warning" and item["details"].get("distribution") == distribution
            ]
            self.assertTrue(warnings, section["details"]["findings"])

    def test_the_json_section_shape_is_stable_for_every_consumer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir, acquisition=(ACQUISITION_PROVIDER_ID,))
            with installed_provider_plugins("import-error"):
                section = self.section(workspace)

        details = section["details"]
        self.assertEqual(
            ["authorization", "counts", "findings", "invalid", "registered"],
            sorted(details),
        )
        self.assertEqual(["acquisition", "discovery"], sorted(details["authorization"]))
        self.assertEqual(
            {
                "enabled": True,
                "providers": [ACQUISITION_PROVIDER_ID],
                "entry_point_group": "evidence_wiki.acquisition_providers",
            },
            details["authorization"]["acquisition"],
        )
        self.assertEqual(
            [
                "authorized",
                "capabilities",
                "distribution",
                "entry_point",
                "entry_point_group",
                "id",
                "phase",
                "provider_api_version",
                "state",
                "version",
            ],
            sorted(details["registered"][0]),
        )
        self.assertEqual(
            ["authorized", "distribution", "entry_point", "entry_point_group", "id", "phase", "reason"],
            sorted(details["invalid"][0]),
        )
        self.assertEqual(
            ["code", "details", "message", "phase", "provider_id", "remediation", "severity"],
            sorted(details["findings"][0]),
        )
        # Serializable as one JSON document, which is what the purity contract requires.
        json.dumps(section)

    def test_declared_credential_names_join_the_secrets_hygiene_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(
                tmpdir,
                acquisition=(ACQUISITION_PROVIDER_ID,),
                env_file=f"{self.CREDENTIAL_NAME}={self.CREDENTIAL_VALUE}\nUNRELATED=x\n",
            )
            with installed_provider_plugins():
                checks = {check["id"]: check for check in self.report(workspace)["checks"]}

        secrets = checks["secret_exposure"]
        self.assertEqual("degraded", secrets["status"])
        self.assertEqual([self.CREDENTIAL_NAME], secrets["details"]["declared_credentials"])
        self.assertEqual([self.CREDENTIAL_NAME], secrets["details"]["exposed_credentials"])
        self.assertIn(self.CREDENTIAL_NAME, secrets["message"])
        # Only *declared* names are named. Doctor has no business enumerating the rest of
        # an operator's environment just because it can read the file.
        self.assertNotIn("UNRELATED", json.dumps(secrets))

    def test_a_credential_value_never_appears_in_any_output(self):
        # The single property the whole capability summary is built to guarantee. The
        # value is in the process environment *and* in a readable .env, which is the
        # worst realistic case, and neither format may echo it.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(
                tmpdir,
                acquisition=(ACQUISITION_PROVIDER_ID,),
                env_file=f"{self.CREDENTIAL_NAME}={self.CREDENTIAL_VALUE}\n",
            )
            with (
                mock.patch.dict(os.environ, {self.CREDENTIAL_NAME: self.CREDENTIAL_VALUE}),
                installed_provider_plugins(),
            ):
                report = self.report(workspace)
                text = self.doctor.render_text(report)

        serialized = json.dumps(report, sort_keys=True)
        for rendered in (serialized, text):
            with self.subTest(format="json" if rendered is serialized else "text"):
                self.assertNotIn(self.CREDENTIAL_VALUE, rendered)
                self.assertIn(self.CREDENTIAL_NAME, rendered)

    def test_the_text_report_answers_what_this_workspace_can_reach_and_what_was_authorized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir, acquisition=(ACQUISITION_PROVIDER_ID,))
            with installed_provider_plugins():
                text = self.doctor.render_text(self.report(workspace))

        expected = (
            "  Authorized in research.yml:",
            f"    acquisition (enabled): {ACQUISITION_PROVIDER_ID}",
            "  Enabled (registered here and authorized in research.yml):",
            "  Available (registered here, not authorized in research.yml):",
            "      Declared domains: api.keepa-fixture.invalid, assets.keepa-fixture.invalid",
            "      Rate limit: 60 request(s) per minute",
            f"      Credential names (values never read): {self.CREDENTIAL_NAME}",
            "      Licence inference: partial",
            "      Request kinds: market-data/price_history",
        )
        for line in expected:
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_a_broken_workspace_still_reports_the_section_rather_than_refusing(self):
        # doctor.py is in the purity harness' REPORTS_ON_A_BROKEN_WORKSPACE set: an
        # unreadable workspace is something it describes, never something it refuses. A
        # broken registration must not turn that into a fatal envelope either.
        with tempfile.TemporaryDirectory() as tmpdir:
            not_a_workspace = Path(tmpdir) / "not-a-workspace"
            not_a_workspace.mkdir()
            stdout = io.StringIO()
            with (
                installed_provider_plugins("import-error", "invalid-declaration"),
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = self.doctor.main(
                    ["--project-root", str(not_a_workspace), "--format", "json"], env=FakeEnvironment()
                )

        report = json.loads(stdout.getvalue())
        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        self.assertEqual(1, exit_code)
        self.assertEqual("missing", report["verdict"])
        # Missing because the workspace has no directories, not because a plugin broke.
        self.assertEqual("degraded", section["status"])
        self.assertTrue(section["details"]["invalid"])
        self.assertEqual({"acquisition": [], "discovery": []},
                         {phase: block["providers"] for phase, block in section["details"]["authorization"].items()})

    def test_an_enumeration_failure_is_a_finding_rather_than_a_crashed_report(self):
        # The loader promises never to raise. If that promise is ever broken, doctor must
        # still produce its report: one plugin defect cannot cost an operator every other
        # answer on the page.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.workspace(tmpdir)
            with mock.patch.object(
                self.doctor, "registration_report", side_effect=RuntimeError("metadata is unreadable")
            ):
                report = self.report(workspace)

        section = {check["id"]: check for check in report["checks"]}["registered_providers"]
        self.assertEqual("degraded", section["status"])
        self.assertFalse(section["required"])
        self.assertIn("metadata is unreadable", section["message"])
        self.assertEqual([], section["details"]["registered"])
        self.assertEqual("degraded", report["verdict"])


if __name__ == "__main__":
    unittest.main()
