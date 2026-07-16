import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("cross_platform_inventory", "source_inventory.py")
NORMALIZE = load_script_module("cross_platform_normalize", "normalize_sources.py")
LINT = load_script_module("cross_platform_lint", "lint.py")
QUERY = load_script_module("cross_platform_query", "query_index.py")
QSTATUS = load_script_module("cross_platform_question_status", "question_status.py")


class CrossPlatformBehaviorTests(unittest.TestCase):
    def build_crlf_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        for path in (
            "raw/links",
            "sources/normalized",
            "wiki/questions",
            "wiki/concepts",
        ):
            (workspace / path).mkdir(parents=True, exist_ok=True)
        (workspace / "research.yml").write_bytes(
            b"project:\r\n"
            b"  name: crlf-cross-platform\r\n"
            b"raw:\r\n"
            b"  source_roots:\r\n"
            b"    - raw/links\r\n"
            b"sources:\r\n"
            b"  manifest_path: sources/manifest.jsonl\r\n"
            b"  normalized_dir: sources/normalized\r\n"
            b"  default_status: discovered\r\n"
            b"  lifecycle_statuses:\r\n"
            b"    - discovered\r\n"
            b"    - normalized\r\n"
            b"    - noted\r\n"
            b"    - integrated\r\n"
            b"    - deferred\r\n"
            b"    - superseded\r\n"
            b"    - rejected\r\n"
            b"wiki:\r\n"
            b"  root: wiki\r\n"
            b"  required_dirs: []\r\n"
            b"  allowed_page_types:\r\n"
            b"    - question\r\n"
            b"    - concept\r\n"
            b"  frontmatter_required: []\r\n"
            b"lint:\r\n"
            b"  validate_structure: false\r\n"
            b"  validate_frontmatter: true\r\n"
            b"  validate_links: false\r\n"
            b"  validate_source_coverage: true\r\n"
            b"  validate_claims: false\r\n"
        )
        (workspace / "log.md").write_bytes(b"# Log\r\n\r\n- CRLF log entry\r\n")
        (workspace / "raw" / "links" / "links.txt").write_bytes(
            b"# links\r\n[Fixture](https://example.org/crlf?x=1#frag)\r\n"
        )
        (workspace / "wiki" / "questions" / "crlf-question.md").write_bytes(
            b"---\r\n"
            b"type: question\r\n"
            b"created: 2026-05-31\r\n"
            b"updated: 2026-05-31\r\n"
            b"source_ids: []\r\n"
            b"status: open\r\n"
            b"priority: medium\r\n"
            b"question: Does CRLF parsing work?\r\n"
            b"---\r\n\r\n"
            b"# CRLF Question\r\n"
        )
        return workspace

    def test_windows_style_config_paths_are_rejected_on_posix(self):
        unsafe_values = [
            "C:\\Users\\research\\wiki",
            "D:/research/wiki",
            "wiki\\..\\outside",
            "\\absolute\\wiki",
        ]
        validators = [
            INVENTORY.validate_workspace_relative_path,
            NORMALIZE.validate_workspace_relative_path,
            QUERY.validate_workspace_relative_path,
        ]
        for value in unsafe_values:
            for validator in validators:
                with self.subTest(value=value, validator=validator.__module__):
                    with self.assertRaises(SystemExit):
                        validator(value, "wiki.root")

    def test_crlf_config_log_links_and_question_pages_are_parsed_consistently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_crlf_workspace(Path(tmpdir))

            records, warnings, _ = INVENTORY.build_records(
                workspace,
                INVENTORY.load_config(workspace),
                previous_detected_at={},
            )
            INVENTORY.write_manifest(workspace / "sources" / "manifest.jsonl", records)
            lint_results = LINT.run_checks(workspace, LINT.load_config(workspace))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = QSTATUS.main(["--project-root", str(workspace), "--format", "json"])
            question_payload = json.loads(stdout.getvalue())

        self.assertEqual([], warnings)
        self.assertEqual(["https://example.org/crlf?x=1#frag"], [record["url"] for record in records])
        self.assertEqual(0, exit_code)
        self.assertEqual(1, question_payload["total"])
        self.assertEqual("open", question_payload["questions"][0]["status"])
        self.assertNotIn("frontmatter", {issue["category"] for issue in lint_results["issues"]})

    def test_template_scripts_have_shebangs_and_are_not_required_to_be_executable(self):
        missing_shebangs: list[str] = []
        executable_scripts: list[str] = []
        for path in sorted(SCRIPTS.glob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            first_line = path.read_text().splitlines()[0]
            if first_line != "#!/usr/bin/env python3":
                missing_shebangs.append(relative)
            if path.stat().st_mode & stat.S_IXUSR:
                executable_scripts.append(relative)

        self.assertEqual([], missing_shebangs)
        self.assertEqual([], executable_scripts)

    def test_case_only_source_names_remain_distinct_where_the_filesystem_supports_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_crlf_workspace(Path(tmpdir))
            upper = workspace / "raw" / "links" / "Case.url"
            lower = workspace / "raw" / "links" / "case.url"
            upper.write_text("[InternetShortcut]\nURL=https://example.org/upper\n", encoding="utf-8")
            lower.write_text("[InternetShortcut]\nURL=https://example.org/lower\n", encoding="utf-8")
            if upper.samefile(lower):
                self.skipTest("case-folding filesystem requires the retained native platform lane")

            records, warnings, _summary = INVENTORY.build_records(
                workspace,
                INVENTORY.load_config(workspace),
                previous_detected_at={},
            )

        paths = {
            raw_path
            for record in records
            for raw_path in record.get("raw_paths", [])
            if raw_path.endswith(("Case.url", "case.url"))
        }
        self.assertEqual({"raw/links/Case.url", "raw/links/case.url"}, paths)
        self.assertFalse([warning for warning in warnings if "Case.url" in warning or "case.url" in warning])

    def test_ci_declares_native_windows_macos_and_ubuntu_jobs(self):
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(f"os: {runner}", workflow)
        self.assertIn('python-version: "3.10"', workflow)

    def test_tests_use_the_platform_temporary_directory(self):
        posix_temp_root = "/" + "tmp"
        posix_only_temporary_directory = f'TemporaryDirectory(dir="{posix_temp_root}")'
        offenders = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in sorted((REPO_ROOT / "tests").glob("test_*.py"))
            if posix_only_temporary_directory in path.read_text(encoding="utf-8")
        ]

        self.assertEqual([], offenders)

    def test_ci_propagates_every_windows_python_failure(self):
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        failure_guard = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"

        for step_name in (
            "Install development environment (Windows)",
            "Run tests and quality checks (Windows)",
        ):
            with self.subTest(step=step_name):
                marker = f"      - name: {step_name}\n"
                self.assertIn(marker, workflow)
                step = workflow.split(marker, 1)[1].split("\n      - name:", 1)[0]
                lines = [line.strip() for line in step.splitlines() if line.strip()]
                python_commands = [
                    index
                    for index, line in enumerate(lines)
                    if line.startswith(("python ", ".\\.venv\\Scripts\\python.exe "))
                ]

                self.assertEqual(3, len(python_commands))
                for index in python_commands:
                    self.assertLess(index + 1, len(lines))
                    self.assertEqual(failure_guard, lines[index + 1])


if __name__ == "__main__":
    unittest.main()
