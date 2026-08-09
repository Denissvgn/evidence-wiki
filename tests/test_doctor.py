import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
