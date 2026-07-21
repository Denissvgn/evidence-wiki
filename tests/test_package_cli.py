import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import cli, resources


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PackageCliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli.main(list(args))
        self.assertEqual(0, exit_code)
        return stdout.getvalue()

    def run_cli_result(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = cli.main(list(args))
        return int(exit_code or 0), stdout.getvalue(), stderr.getvalue()

    def smoke_results(self, target: Path) -> dict:
        smoke = load_script_module("evidence_wiki_package_cli_smoke", target / "scripts" / "smoke_validate_workspace.py")
        return smoke.run_checks(target)

    def test_assets_resolve_from_source_checkout(self):
        with resources.assets_root() as root:
            self.assertTrue((root / "workspace-template" / "research.yml").is_file())
            self.assertTrue((root / "domain-packs" / "llm-research" / "taxonomy.md").is_file())
            self.assertTrue(resources.orchestrator_skill_path(root).is_file())

    def test_orchestrator_guide_reports_skill_path_and_content(self):
        path_output = self.run_cli("orchestrator-guide").strip()
        self.assertTrue(Path(path_output).is_file(), path_output)
        self.assertEqual("research-orchestrate.md", Path(path_output).name)

        content = self.run_cli("orchestrator-guide", "--print")
        self.assertIn("# research-orchestrate", content)
        self.assertIn("## Workflow", content)

        payload = json.loads(self.run_cli("orchestrator-guide", "--format", "json"))
        self.assertEqual("research-orchestrate", payload["skill"])
        self.assertEqual(cli.__version__, payload["package_version"])
        self.assertEqual("research-orchestrate.md", Path(payload["path"]).name)

    def test_orchestrator_guide_rejects_unknown_format(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["orchestrator-guide", "--format", "yaml"])

    def test_contract_command_reports_supported_schema_versions(self):
        output = self.run_cli("contract")
        payload = json.loads(output)

        self.assertEqual("evidence-wiki", payload["package"])
        self.assertEqual(cli.__version__, payload["package_version"])
        self.assertEqual("1.0", payload["schema_version"])
        self.assertIsInstance(payload["starter_version"], str)
        self.assertIsInstance(payload["compatible_research_yml_contract"], str)
        self.assertEqual(["0.1"], payload["profile_schema_versions"])
        self.assertEqual(
            {
                "workspace_schema_versions": ["0.1"],
                "research_yml_contract_versions": ["0.1"],
            },
            payload["upgrade_compatibility"],
        )
        required_assets = payload["required_asset_manifest"]
        self.assertIn("workspace-template/AGENTS.md", required_assets["starter"])
        self.assertIn("workspace-template/README.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/orchestration.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/orchestrator-handoff.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/run-controller.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/acquisition.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/source-discovery.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/source-delivery.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/workspace-status.md", required_assets["starter"])
        self.assertIn("workspace-template/docs/workspace-system.md", required_assets["starter"])
        self.assertIn("workspace-template/scripts/discover_sources.py", required_assets["starter"])
        self.assertIn("workspace-template/scripts/fetch_sources.py", required_assets["starter"])
        self.assertIn("workspace-template/scripts/run_controller.py", required_assets["starter"])
        self.assertIn("workspace-template/scripts/source_requests.py", required_assets["starter"])
        self.assertIn("workspace-template/skills/research-run.md", required_assets["starter"])
        self.assertIn("workspace-template/skills/research-discover.md", required_assets["starter"])
        self.assertIn("workspace-template/skills/research-acquire.md", required_assets["starter"])
        self.assertIn("workspace-template/skills/research-verify.md", required_assets["starter"])
        self.assertIn(
            "domain-packs/standards-compliance/research.overlay.yml",
            required_assets["domain_packs"],
        )
        self.assertIn("orchestrator/skills/research-orchestrate.md", required_assets["orchestrator"])
        self.assertEqual("1.0", payload["artifact_schemas"]["workspace_status"])
        self.assertEqual("1.0", payload["artifact_schemas"]["question_intake"])
        self.assertEqual("1.0", payload["artifact_schemas"]["answer_export"])
        self.assertEqual("1.0", payload["artifact_schemas"]["source_requests"])
        self.assertEqual("1.0", payload["artifact_schemas"]["fetch_sources"])
        self.assertEqual("1.0", payload["artifact_schemas"]["citation_verification"])
        self.assertEqual("1.0", payload["artifact_schemas"]["quote_verification"])
        self.assertEqual("1.0", payload["artifact_schemas"]["discover_sources"])
        self.assertEqual("1.0", payload["artifact_schemas"]["mcp_server"])
        self.assertEqual("1.0", payload["artifact_schemas"]["question_resolve"])
        self.assertEqual("1.0", payload["artifact_schemas"]["run_state"])
        self.assertEqual("1.0", payload["artifact_schemas"]["orchestration_attempt"])
        self.assertEqual("1.0", payload["artifact_schemas"]["coverage_manifest"])
        self.assertEqual("1.0", payload["artifact_schemas"]["publication_readiness"])
        self.assertEqual("1.0", payload["artifact_schemas"]["error_envelope"])
        self.assertEqual(
            {
                "evidence_paths": [
                    "academic_method_existence",
                    "github_implementation",
                    "legal_current_figure",
                    "official_guidance",
                    "product_requirement_profile",
                    "standards_registry_reference",
                    "vendor_product_spec",
                ],
                "source_policy": [
                    "academic_indexed",
                    "canonical_repository",
                    "domain_pack_allowed",
                    "manual_review_required",
                    "official_primary",
                    "official_standards_registry",
                    "official_vendor",
                    "openalex_or_arxiv",
                    "primary_or_official",
                    "standards_body_primary",
                ],
                "freshness_policy": [
                    "current_legal_figure",
                    "current_product_requirement",
                    "current_product_spec",
                    "current_standard_reference",
                    "manual_review",
                    "no_staleness_check",
                    "pack:general-science/study-recency",
                    "publication_identity",
                    "release_snapshot",
                ],
                "identity_policy": [
                    "citation_id_resolves",
                    "none",
                    "official_domain_match",
                    "origin_url_matches_candidate",
                    "registry_entry_matches_product_requirement",
                    "repo_ref_resolves",
                    "standard_designation_matches_registry",
                ],
                "artifact_kinds": [
                    "release_metadata",
                    "repository_metadata",
                    "source_archive",
                ],
            },
            payload["policy_vocabularies"],
        )
        definitions = payload["policy_vocabulary_definitions"]
        self.assertIn("base", definitions)
        self.assertIn("installed_domain_packs", definitions)
        self.assertIn("merged", definitions)
        self.assertIn(
            "pack:general-science/study-recency",
            definitions["installed_domain_packs"]["general-science"]["freshness_policy"],
        )
        self.assertIn(
            "Require a reviewer",
            definitions["merged"]["freshness_policy"]["pack:general-science/study-recency"],
        )

        with resources.assets_root() as root:
            metadata = yaml.safe_load((root / "workspace-template" / "workspace-system.yml").read_text())
        workspace_system = metadata["workspace_system"]
        self.assertEqual(workspace_system["starter_version"], payload["starter_version"])
        self.assertEqual(
            workspace_system["compatible_research_yml_contract"],
            payload["compatible_research_yml_contract"],
        )

    def test_doctor_command_reports_environment_as_json(self):
        output = self.run_cli("doctor", "--format", "json")
        payload = json.loads(output)

        self.assertEqual("1.0", payload["schema_version"])
        self.assertIn(payload["verdict"], {"ok", "degraded"})
        checks = {check["id"]: check for check in payload["checks"]}
        for check_id in ("python", "pyyaml", "pdftotext", "git", "workspace_write", "contract"):
            self.assertIn(check_id, checks)
        self.assertEqual("ok", checks["python"]["status"])
        self.assertEqual("ok", checks["pyyaml"]["status"])

    def test_doctor_target_maps_to_workspace_project_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "doctor-cli-workspace",
                "--project-description",
                "Workspace for doctor CLI checks.",
            )

            output = self.run_cli("doctor", "--target", str(target), "--format", "json")

        payload = json.loads(output)
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual(target.resolve().as_posix(), payload["project_root"])
        self.assertEqual("ok", checks["contract"]["status"])
        self.assertIsInstance(checks["contract"]["details"]["starter_version"], str)

    def test_doctor_cli_matches_copied_workspace_script_json_and_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "doctor parity Ω workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "doctor-parity-workspace",
                "--project-description",
                "Doctor parity workspace.",
            )
            cli_code, cli_stdout, cli_stderr = self.run_cli_result(
                "doctor", "--target", str(target), "--format", "json"
            )
            direct = subprocess.run(
                [
                    sys.executable,
                    str(target / "scripts" / "doctor.py"),
                    "--project-root",
                    str(target),
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(direct.returncode, cli_code)
        self.assertEqual("", cli_stderr)
        self.assertEqual("", direct.stderr)
        cli_document = json.loads(cli_stdout)
        direct_document = json.loads(direct.stdout)
        cli_document.pop("generated_at", None)
        direct_document.pop("generated_at", None)
        self.assertEqual(direct_document, cli_document)

    def test_fleet_status_command_aggregates_targets_as_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "fleet-cli-workspace",
                "--project-description",
                "Workspace for fleet-status CLI checks.",
            )

            output = self.run_cli("fleet-status", "--target", str(target), "--format", "json")

        payload = json.loads(output)
        self.assertEqual("1.0", payload["schema_version"])
        self.assertEqual(1, len(payload["targets"]))
        self.assertTrue(payload["targets"][0]["ok"])
        self.assertEqual(str(target.resolve()), payload["targets"][0]["path"])
        self.assertEqual("fleet-cli-workspace", payload["targets"][0]["project_name"])

    def test_status_cli_matches_workspace_script_for_unicode_spaced_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace Ω with spaces"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "interface-parity-workspace",
                "--project-description",
                "Workspace for installed CLI and copied-script parity.",
            )

            cli_code, cli_stdout, cli_stderr = self.run_cli_result(
                "status",
                "--target",
                str(target),
                "--format",
                "json",
                "--no-cache",
            )
            direct = subprocess.run(
                [
                    sys.executable,
                    str(target / "scripts" / "workspace_status.py"),
                    "--project-root",
                    str(target),
                    "--format",
                    "json",
                    "--no-cache",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, cli_code, cli_stderr)
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertEqual("", cli_stderr)
        cli_document = json.loads(cli_stdout)
        direct_document = json.loads(direct.stdout)
        for field in (
            "schema_version",
            "project",
            "contract",
            "questions",
            "coverage",
            "candidates",
            "sources",
            "lint",
            "readiness",
            "workspace_health",
        ):
            self.assertEqual(cli_document[field], direct_document[field], field)
        self.assertEqual(target.resolve().as_posix(), cli_document["workspace_health"]["project_root"])

    def test_export_alias_matches_questions_export_schema_and_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "export-workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "export-interface-parity",
                "--project-description",
                "Workspace for export interface parity.",
            )

            alias_code, alias_stdout, alias_stderr = self.run_cli_result(
                "export", "--target", str(target), "--format", "json"
            )
            nested_code, nested_stdout, nested_stderr = self.run_cli_result(
                "questions", "export", "--target", str(target), "--format", "json"
            )
            direct = subprocess.run(
                [
                    sys.executable,
                    str(target / "scripts" / "export_answers.py"),
                    "--project-root",
                    str(target),
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, alias_code, alias_stderr)
        self.assertEqual(0, nested_code, nested_stderr)
        self.assertEqual("", alias_stderr)
        self.assertEqual("", nested_stderr)
        alias_document = json.loads(alias_stdout)
        nested_document = json.loads(nested_stdout)
        alias_generated_at = alias_document.pop("generated_at")
        nested_generated_at = nested_document.pop("generated_at")
        self.assertRegex(alias_generated_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertRegex(nested_generated_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(alias_document, nested_document)
        self.assertEqual(alias_code, direct.returncode, direct.stderr)
        self.assertEqual("", direct.stderr)
        direct_document = json.loads(direct.stdout)
        direct_document.pop("generated_at", None)
        self.assertEqual(direct_document, alias_document)
        self.assertEqual("1.0", alias_document["schema_version"])

    def test_questions_add_cli_matches_copied_script_json_exit_and_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cli_target = root / "cli-intake"
            script_target = root / "script-intake"
            for target in (cli_target, script_target):
                self.run_cli(
                    "init",
                    "--target",
                    str(target),
                    "--project-name",
                    target.name,
                    "--project-description",
                    "Question intake parity workspace.",
                )
            batch = root / "question-batch.json"
            batch.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {
                                "question": "Which deterministic interface parity checks are required?",
                                "priority": "high",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            cli_code, cli_stdout, cli_stderr = self.run_cli_result(
                "questions",
                "add",
                "--target",
                str(cli_target),
                "--from-file",
                str(batch),
                "--format",
                "json",
            )
            direct = subprocess.run(
                [
                    sys.executable,
                    str(script_target / "scripts" / "intake_questions.py"),
                    "--project-root",
                    str(script_target),
                    "--from-file",
                    str(batch),
                    "--format",
                    "json",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            slug = "which-deterministic-interface-parity-checks-are-required"
            cli_page = (cli_target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")
            direct_page = (script_target / "wiki" / "questions" / f"{slug}.md").read_text(encoding="utf-8")

        self.assertEqual(direct.returncode, cli_code)
        self.assertEqual("", cli_stderr)
        self.assertEqual("", direct.stderr)
        cli_document = json.loads(cli_stdout)
        direct_document = json.loads(direct.stdout)
        cli_document.pop("generated_at", None)
        direct_document.pop("generated_at", None)
        self.assertEqual(direct_document, cli_document)
        self.assertEqual(cli_page, direct_page)

    def test_status_invalid_target_returns_machine_document_and_unreadable_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "missing-workspace"
            code, stdout, stderr = self.run_cli_result(
                "status", "--target", str(target), "--format", "json", "--no-cache"
            )
            with resources.assets_root() as assets:
                direct = subprocess.run(
                    [
                        sys.executable,
                        str(assets / "workspace-template" / "scripts" / "workspace_status.py"),
                        "--project-root",
                        str(target),
                        "--format",
                        "json",
                        "--no-cache",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        self.assertEqual(code, direct.returncode)
        self.assertEqual("", direct.stderr)
        document = json.loads(stdout)
        direct_document = json.loads(direct.stdout)
        self.assertEqual("invalid", document["workspace_health"]["status"])
        self.assertFalse(document["workspace_health"]["materially_valid"])
        self.assertEqual(
            document["workspace_health"]["finding_codes"],
            direct_document["workspace_health"]["finding_codes"],
        )

    def test_status_resolves_supported_symlink_alias_to_canonical_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "canonical-workspace"
            alias = root / "workspace-alias"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "canonical-interface-workspace",
                "--project-description",
                "Workspace for canonical alias checks.",
            )
            try:
                alias.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks are unavailable on this platform: {exc}")

            code, stdout, stderr = self.run_cli_result(
                "status", "--target", str(alias), "--format", "json", "--no-cache"
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        document = json.loads(stdout)
        self.assertEqual(target.resolve().as_posix(), document["workspace_health"]["project_root"])

    def test_status_relative_absolute_and_case_variant_target_identity_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "Case Sensitive Ω Workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "target-identity-workspace",
                "--project-description",
                "Canonical project-root parity workspace.",
            )
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                relative_code, relative_stdout, relative_stderr = self.run_cli_result(
                    "status", "--target", target.name, "--format", "json", "--no-cache"
                )
            finally:
                os.chdir(previous_cwd)
            absolute_code, absolute_stdout, absolute_stderr = self.run_cli_result(
                "status", "--target", str(target), "--format", "json", "--no-cache"
            )
            case_variant = root / target.name.swapcase()
            case_code, case_stdout, case_stderr = self.run_cli_result(
                "status", "--target", str(case_variant), "--format", "json", "--no-cache"
            )
            canonical = target.resolve().as_posix()
            case_variant_is_same_target = case_variant.exists() and case_variant.samefile(target)
            case_document = json.loads(case_stdout)
            if case_variant_is_same_target:
                reported_root = Path(case_document["workspace_health"]["project_root"])
                self.assertTrue(reported_root.samefile(target))

        self.assertEqual(0, relative_code, relative_stderr)
        self.assertEqual(0, absolute_code, absolute_stderr)
        relative = json.loads(relative_stdout)
        absolute = json.loads(absolute_stdout)
        self.assertEqual(canonical, relative["workspace_health"]["project_root"])
        self.assertEqual(canonical, absolute["workspace_health"]["project_root"])
        self.assertEqual(relative["project"], absolute["project"])
        self.assertEqual("", case_stderr)
        if case_variant_is_same_target:
            self.assertEqual(0, case_code)
        else:
            self.assertEqual(2, case_code)
            self.assertEqual("invalid", case_document["workspace_health"]["status"])

    def test_package_serve_mcp_matches_copied_stdio_server_and_keeps_stdout_protocol_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "MCP parity Ω workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "mcp-cli-parity",
                "--project-description",
                "Package and copied MCP stdio parity workspace.",
            )
            protocol_input = "\n".join(
                (
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                )
            ) + "\n"
            package_stdout = io.StringIO()
            package_stderr = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(protocol_input)):
                with contextlib.redirect_stdout(package_stdout), contextlib.redirect_stderr(package_stderr):
                    package_code = cli.main(["serve-mcp", "--target", str(target)])
            direct = subprocess.run(
                [sys.executable, str(target / "scripts" / "serve_mcp.py"), "--target", str(target)],
                input=protocol_input,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(0, package_code)
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertEqual("", package_stderr.getvalue())
        self.assertEqual("", direct.stderr)
        package_messages = [json.loads(line) for line in package_stdout.getvalue().splitlines() if line.strip()]
        direct_messages = [json.loads(line) for line in direct.stdout.splitlines() if line.strip()]
        self.assertEqual(direct_messages, package_messages)
        self.assertEqual([1, 2], [message["id"] for message in package_messages])

    def test_init_command_creates_smoke_valid_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"

            output = self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "package-workspace",
                "--project-description",
                "Workspace created through the package CLI.",
            )

            self.assertIn("Created research workspace", output)
            self.assertTrue((target / "scripts" / "smoke_validate_workspace.py").is_file())
            self.assertTrue((target / "docs" / "workspace-initialization.md").is_file())
            self.assertTrue((target / "skills" / "research-init.md").is_file())
            self.assertTrue((target / "skills" / "domain-pack-create.md").is_file())
            # Orchestrator skills target the external parent agent and must not be
            # copied into a created workspace.
            self.assertFalse((target / "skills" / "research-orchestrate.md").exists())
            self.assertFalse((target / "orchestrator").exists())
            self.assertTrue((target / "raw" / "papers").is_dir())
            self.assertTrue((target / "sources" / "normalized").is_dir())
            self.assertTrue((target / "wiki" / "sources").is_dir())
            self.assertFalse((target / "pilot-workspaces").exists())
            self.assertFalse((target / "reports").exists())

            results = self.smoke_results(target)
            self.assertTrue(results["ok"], results["issues"])
            self.assertEqual([], results["issues"])

    def test_deploy_alias_resolves_packaged_domain_pack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"

            self.run_cli(
                "deploy",
                "--target",
                str(target),
                "--project-name",
                "package-domain-workspace",
                "--project-description",
                "Workspace created with a packaged domain pack.",
                "--domain-pack",
                "llm-research",
            )

            config = yaml.safe_load((target / "research.yml").read_text())
            self.assertEqual("llm-research", config["domain_pack"]["name"])
            self.assertEqual(
                "domain-packs/llm-research/coverage-templates/academic-method-feasibility.yml",
                config["domain_pack"]["coverage_templates"]["academic-method-feasibility"],
            )
            self.assertTrue((target / "domain-packs" / "llm-research" / "taxonomy.md").is_file())
            self.assertTrue(
                (target / "domain-packs" / "llm-research" / "coverage-templates" / "vendor-product-spec.yml").is_file()
            )
            results = self.smoke_results(target)
            self.assertTrue(results["ok"], results["issues"])

    def test_questions_add_and_export_round_trip_through_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "questions-cli-workspace",
                "--project-description",
                "Workspace for question API CLI checks.",
            )

            batch_path = Path(tmpdir) / "batch.yaml"
            batch_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "questions": [{"question": "What benchmarks matter?", "priority": "high"}],
                    },
                    sort_keys=False,
                )
            )

            add_output = self.run_cli(
                "questions",
                "add",
                "--target",
                str(target),
                "--from-file",
                str(batch_path),
                "--format",
                "json",
            )
            add_report = json.loads(add_output)
            self.assertEqual(1, add_report["counts"]["created"])
            self.assertTrue((target / "wiki" / "questions" / "what-benchmarks-matter.md").is_file())

            export_output = self.run_cli("questions", "export", f"--target={target}")
            export_document = json.loads(export_output)
            self.assertEqual("1.0", export_document["schema_version"])
            self.assertEqual(1, export_document["counts"]["total"])
            self.assertEqual(
                ["what-benchmarks-matter"],
                [record["slug"] for record in export_document["questions"]],
            )

    def test_questions_add_limit_failure_returns_typed_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "questions-cli-cap-workspace",
                "--project-description",
                "Workspace for capped question API CLI checks.",
            )
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text())
            config["run"]["max_open_questions_total"] = 1
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))

            batch_path = Path(tmpdir) / "batch.yaml"
            batch_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {"question": "What benchmarks matter?"},
                            {"question": "Which datasets matter?"},
                        ],
                    },
                    sort_keys=False,
                )
            )

            code, stdout, stderr = self.run_cli_result(
                "questions",
                "add",
                "--target",
                str(target),
                "--from-file",
                str(batch_path),
                "--format",
                "json",
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("INTAKE_TOTAL_CAP_EXCEEDED", json.loads(stderr)["error_code"])
            self.assertFalse((target / "wiki" / "questions" / "what-benchmarks-matter.md").exists())
            self.assertFalse((target / "wiki" / "questions" / "which-datasets-matter.md").exists())

    def test_questions_add_field_cap_failure_returns_typed_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "workspace"
            self.run_cli(
                "init",
                "--target",
                str(target),
                "--project-name",
                "questions-cli-field-cap-workspace",
                "--project-description",
                "Workspace for field cap question API checks.",
            )
            batch_path = Path(tmpdir) / "batch.yaml"
            batch_path.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {
                                "question": "What benchmarks matter?",
                                "context": "C" * 8193,
                            }
                        ],
                    },
                    sort_keys=False,
                )
            )

            code, stdout, stderr = self.run_cli_result(
                "questions",
                "add",
                "--target",
                str(target),
                "--from-file",
                str(batch_path),
                "--format",
                "json",
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("INTAKE_FIELD_TOO_LONG", envelope["error_code"])
            self.assertEqual(
                {
                    "item_index": 0,
                    "field": "context",
                    "actual_bytes": 8193,
                    "max_bytes": 8192,
                },
                envelope["details"]["violations"][0],
            )
            self.assertFalse((target / "wiki" / "questions" / "what-benchmarks-matter.md").exists())

    def test_questions_help_lists_subcommands(self):
        output = self.run_cli("questions", "--help")
        self.assertIn("questions add", output)
        self.assertIn("questions export", output)

    def test_pack_validate_command_reports_ok_json(self):
        output = self.run_cli("pack", "validate", "--path", "llm-research")
        payload = json.loads(output)

        self.assertTrue(payload["ok"], payload)
        self.assertEqual("llm-research", payload["domain_pack"]["name"])
        self.assertTrue(payload["smoke_validation"]["ok"], payload["smoke_validation"]["issues"])

    def test_all_shipped_domain_packs_validate(self):
        for pack in resources.REQUIRED_DOMAIN_PACKS:
            with self.subTest(pack=pack):
                output = self.run_cli("pack", "validate", "--path", pack)
                payload = json.loads(output)
                self.assertTrue(payload["ok"], payload)
                self.assertEqual(pack, payload["domain_pack"]["name"])

    def test_pack_validate_reports_declared_policy_vocabularies(self):
        output = self.run_cli("pack", "validate", "--path", "general-science")
        payload = json.loads(output)

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            {
                "pack:general-science/study-recency": (
                    "Require a reviewer to confirm that study dates, dataset releases, "
                    "and follow-up literature are recent enough for the scientific question."
                )
            },
            payload["domain_pack"]["policy_vocabularies"]["freshness_policy"],
        )
        checks = {check["id"]: check for check in payload["checks"]}
        self.assertEqual("pass", checks["policy_vocabularies"]["status"])

    def test_help_mentions_doctor(self):
        output = self.run_cli("--help")
        self.assertIn("evidence-wiki doctor", output)
        self.assertIn("evidence-wiki fleet-status", output)
        self.assertIn("evidence-wiki serve-mcp", output)
        self.assertIn("evidence-wiki pack validate", output)
        self.assertIn("--scope-root PATH", output)

    def test_pyproject_includes_standalone_product_paths(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        self.assertIn('"/examples"', pyproject)
        self.assertIn('"/CHANGELOG.md"', pyproject)
        self.assertIn('"/LICENSE"', pyproject)
        self.assertIn('"workspace-template" = "evidence_wiki/assets/workspace-template"', pyproject)
        self.assertIn('"domain-packs" = "evidence_wiki/assets/domain-packs"', pyproject)

    def test_pyproject_publishes_public_repository_metadata(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()

        self.assertIn('license = { text = "MIT" }', pyproject)
        self.assertIn("[project.urls]", pyproject)
        self.assertIn('Repository = "https://github.com/Denissvgn/evidence-wiki"', pyproject)
        self.assertIn('Issues = "https://github.com/Denissvgn/evidence-wiki/issues"', pyproject)


if __name__ == "__main__":
    unittest.main()
