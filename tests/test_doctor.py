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
        "  starter_version: \"0.4.0\"\n"
        "  schema_version: \"0.1\"\n"
        "  compatible_research_yml_contract: \"0.1\"\n"
    )
    return workspace


class FakeEnvironment:
    def __init__(self, *, python_version=(3, 11, 0), yaml_error: Exception | None = None):
        self.python_version = python_version
        self.yaml_error = yaml_error

    def import_yaml(self):
        if self.yaml_error is not None:
            raise self.yaml_error
        import yaml

        return yaml

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
        self.assertEqual("ok", checks["pdftotext"]["status"])
        self.assertEqual("ok", checks["git"]["status"])
        self.assertEqual("ok", checks["workspace_write"]["status"])
        self.assertEqual("ok", checks["contract"]["status"])
        self.assertEqual("ok", checks["semantic_retrieval"]["status"])
        self.assertEqual("ok", checks["secret_exposure"]["status"])
        self.assertEqual("0.4.0", checks["contract"]["details"]["starter_version"])
        self.assertEqual("0.1", checks["contract"]["details"]["schema_version"])
        self.assertEqual("0.1", checks["contract"]["details"]["compatible_research_yml_contract"])
        self.assertEqual(
            ["docs", "raw", "root", "scripts", "sources", "wiki"],
            sorted(checks["workspace_write"]["details"]["checked"]),
        )

    def test_missing_optional_tools_degrade_with_path_manipulation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = make_workspace(Path(tmpdir))
            with mock.patch.dict(os.environ, {"PATH": ""}):
                report = self.doctor.build_report(workspace)

        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual("degraded", report["verdict"])
        self.assertEqual("missing", checks["pdftotext"]["status"])
        self.assertFalse(checks["pdftotext"]["required"])
        self.assertIn("PDF normalization degrades", checks["pdftotext"]["implication"])
        self.assertEqual("missing", checks["git"]["status"])
        self.assertFalse(checks["git"]["required"])
        self.assertIn("version-control", checks["git"]["implication"])

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


if __name__ == "__main__":
    unittest.main()
