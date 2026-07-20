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


if __name__ == "__main__":
    unittest.main()
