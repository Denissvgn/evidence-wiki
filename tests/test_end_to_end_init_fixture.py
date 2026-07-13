import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "end-to-end-init-project"
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


INIT = load_script_module("research_workspace_init_for_e2e_tests", INIT_SCRIPT_PATH)
SMOKE = load_script_module("research_workspace_smoke_for_e2e_tests", SMOKE_SCRIPT_PATH)


class EndToEndInitFixtureTests(unittest.TestCase):
    def load_fixture_profile(self, target: Path) -> dict:
        profile = yaml.safe_load((FIXTURE_ROOT / "workspace-init.yml").read_text())
        profile["workspace_init"]["target_path"] = str(target)
        return profile

    def write_profile(self, profile_path: Path, target: Path) -> None:
        profile = self.load_fixture_profile(target)
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    def run_init(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = INIT.main(list(args))
        return exit_code, stdout.getvalue()

    def load_config(self, target: Path) -> dict:
        return yaml.safe_load((target / "research.yml").read_text())

    def test_fixture_prompt_records_minimal_request(self):
        prompt = (FIXTURE_ROOT / "prompt.md").read_text()

        self.assertIn("autonomous LLM research systems", prompt)
        self.assertIn("initial sources", prompt)
        self.assertIn("implementation", prompt)
        self.assertIn("evidence", prompt)

    def test_minimal_profile_creates_smoke_valid_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "workspace"
            profile_path = root / "workspace-init.yml"
            self.write_profile(profile_path, target)

            exit_code, output = self.run_init("--profile", str(profile_path))

            self.assertEqual(0, exit_code)
            self.assertIn("Workspace initialization plan", output)
            self.assertIn("Created research workspace", output)

            config = self.load_config(target)
            self.assertEqual("autonomous-llm-systems-research", config["project"]["name"])
            self.assertEqual("en", config["project"]["language"])
            self.assertEqual(["raw/papers", "raw/links", "raw/code"], config["raw"]["source_roots"])
            self.assertEqual(["markdown", "csv", "json", "presentation_outline"], config["outputs"]["supported_formats"])
            self.assertTrue(config["ingest"]["claim_extraction"])
            self.assertTrue(config["lint"]["validate_claims"])
            self.assertEqual("explicit", config["integrations"]["git"]["snapshot_user_edits"])
            codebase = config["integrations"]["codebase_analysis"]
            self.assertTrue(codebase["enabled"])
            self.assertEqual("agent-wiki-cli", codebase["provider"])
            self.assertEqual("sources/code_wikis", codebase["output_dir"])
            self.assertTrue(codebase["read_only"])
            self.assertFalse(codebase["install_hooks"])
            self.assertFalse(codebase["background_sync"])
            self.assertEqual("acknowledged", codebase["untrusted_input"])

            self.assertTrue((target / "raw" / "papers").is_dir())
            self.assertTrue((target / "raw" / "links").is_dir())
            self.assertTrue((target / "raw" / "code").is_dir())
            self.assertTrue((target / "sources" / "code_wikis").is_dir())
            self.assertTrue((target / "docs" / "workspace-init-report.md").is_file())
            self.assertTrue((target / "domain-packs" / "llm-research" / "taxonomy.md").is_file())

            smoke_results = SMOKE.run_checks(target)
            self.assertTrue(smoke_results["ok"], smoke_results["issues"])
            self.assertEqual([], smoke_results["issues"])

    def test_init_report_preserves_minimal_preparation_audit_surface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "workspace"
            profile_path = root / "workspace-init.yml"
            self.write_profile(profile_path, target)

            self.run_init("--profile", str(profile_path))

            report = (target / "docs" / "workspace-init-report.md").read_text()
            self.assertIn("# autonomous-llm-systems-research Init Report", report)
            self.assertIn("Target workspace path or short project name?", report)
            self.assertIn("What research scope and outcome should this workspace support?", report)
            self.assertIn("source_roots: raw/papers, raw/links, raw/code", report)
            self.assertIn("domain_guidance: Use the reusable llm-research domain pack", report)
            self.assertIn("codebase_analysis: Treat the supplied repository", report)
            self.assertIn("raw/papers/autonomous-llm-systems.pdf", report)
            self.assertIn("https://github.com/example/autonomous-llm-system", report)
            self.assertIn("raw/code/autonomous-llm-system", report)
            self.assertIn("Mode: `domain_pack`", report)
            self.assertIn("Reusable domain pack: `llm-research`", report)
            self.assertIn("codebase_analysis.enabled: true", report)
            self.assertIn("codebase_analysis.output_dir: sources/code_wikis", report)
            self.assertIn("codebase_analysis.untrusted_input: acknowledged", report)
            self.assertIn("llm-wiki context --src-dir raw/code/autonomous-llm-system", report)
            self.assertIn("python3 scripts/smoke_validate_workspace.py --format text", report)
            self.assertIn("`pending` - Run after workspace creation.", report)
            self.assertIn("Repository analysis is optional source evidence", report)
            self.assertIn("No codebase-analysis adapter execution during initialization", report)
            self.assertIn("Normalize source records before creating maintained wiki pages", report)

    def test_minimal_profile_dry_run_does_not_create_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "workspace"
            profile_path = root / "workspace-init.yml"
            self.write_profile(profile_path, target)

            exit_code, output = self.run_init("--profile", str(profile_path), "--dry-run")

            self.assertEqual(0, exit_code)
            self.assertIn("writes: none", output)
            self.assertIn("init report: docs/workspace-init-report.md", output)
            self.assertIn(str(profile_path), output)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
