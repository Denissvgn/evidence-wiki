import ast
import contextlib
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
HANDOFF_DOC = REPO_ROOT / "workspace-template" / "docs" / "orchestrator-handoff.md"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"
HELPER_PATH = SCRIPTS / "_script_errors.py"
ERROR_HELPER_CALLS = {"handle_system_exit", "emit_error", "error_envelope"}

JSON_MODE_SCRIPTS = [
    "discover_sources.py",
    "doctor.py",
    "export_answers.py",
    "intake_questions.py",
    "lint.py",
    "query_index.py",
    "question_claim.py",
    "question_resolve.py",
    "question_status.py",
    "run_controller.py",
    "run_report.py",
    "smoke_validate_workspace.py",
    "fetch_sources.py",
    "verify_citations.py",
    "normalize_sources.py",
    "source_inventory.py",
    "source_requests.py",
    "workspace_status.py",
]


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_helper():
    if not HELPER_PATH.is_file():
        raise AssertionError("workspace-template/scripts/_script_errors.py is missing")
    return load_script_module("research_script_errors", "_script_errors.py")


def documented_json_output_script_rows() -> list[tuple[str, str]]:
    text = HANDOFF_DOC.read_text(encoding="utf-8")
    marker = "#### JSON Output Scripts"
    if marker not in text:
        raise AssertionError("orchestrator-handoff.md must include a '#### JSON Output Scripts' table")
    section = text.split(marker, 1)[1].split("\n### ", 1)[0]
    rows: list[tuple[str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or cells[0].lower() == "script":
            continue
        match = re.search(r"`(?:scripts/)?([\w_]+\.py)`", cells[0])
        if match:
            rows.append((match.group(1), cells[2] if len(cells) > 2 else ""))
    if not rows:
        raise AssertionError("JSON Output Scripts table must list at least one workspace script")
    return rows


def documented_json_output_scripts() -> list[str]:
    return [script for script, _codes in documented_json_output_script_rows()]


def documented_json_output_error_codes() -> set[str]:
    codes: set[str] = set()
    for _script, code_cell in documented_json_output_script_rows():
        codes.update(re.findall(r"`([A-Z][A-Z0-9_]+)`", code_cell))
    if not codes:
        raise AssertionError("JSON Output Scripts table must list fatal error codes")
    return codes


def documented_stable_error_codes() -> set[str]:
    text = HANDOFF_DOC.read_text(encoding="utf-8")
    marker = "Stable error codes:"
    if marker not in text:
        raise AssertionError("orchestrator-handoff.md must document stable error codes")
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]
    codes: set[str] = set()
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or cells[0].lower() == "code":
            continue
        match = re.fullmatch(r"`([A-Z][A-Z0-9_]+)`", cells[0])
        if match:
            codes.add(match.group(1))
    if not codes:
        raise AssertionError("Stable error codes table must list at least one code")
    return codes


def script_imports_error_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "_script_errors":
            return True
        if isinstance(node, ast.Import):
            if any(alias.name == "_script_errors" for alias in node.names):
                return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_workspace_module":
            if any(isinstance(argument, ast.Constant) and argument.value == "_script_errors" for argument in node.args):
                return True
    return False


def script_calls_error_helper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in ERROR_HELPER_CALLS:
            return True
        if isinstance(function, ast.Attribute) and function.attr in ERROR_HELPER_CALLS:
            return True
    return False


class ErrorEnvelopeTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        init = load_script_module("error_envelope_init", "init_research_workspace.py")
        target = root / "claim-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"}
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            init.main(["--profile", str(profile_path)])
        return target

    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_helper_builds_contract_shape(self):
        helper = load_helper()

        envelope = helper.error_envelope(
            "CONFIG_MISSING",
            "Missing config: /workspace/research.yml",
            recoverable=True,
            remediation="Run from an initialized workspace or pass --project-root to one.",
        )

        self.assertEqual(
            {
                "schema_version": "1.0",
                "error_code": "CONFIG_MISSING",
                "message": "Missing config: /workspace/research.yml",
                "recoverable": True,
                "remediation": "Run from an initialized workspace or pass --project-root to one.",
            },
            envelope,
        )

    def test_helper_classifies_known_system_exit_messages(self):
        helper = load_helper()

        cases = {
            "PyYAML is required to read research.yml": "DEPENDENCY_MISSING",
            "Missing config: /tmp/workspace/research.yml": "CONFIG_MISSING",
            "Invalid config: /tmp/workspace/research.yml": "CONFIG_INVALID",
            "Missing manifest: /tmp/workspace/sources/manifest.jsonl": "MANIFEST_MISSING",
            "Invalid JSONL in /tmp/workspace/sources/manifest.jsonl:1": "MANIFEST_INVALID",
            (
                "PDF text extraction requires `pdftotext` from Poppler. "
                "Install Poppler or poppler-utils, then rerun normalize_sources.py."
            ): "DEPENDENCY_MISSING",
            "Missing baseline file: /tmp/run-baseline.json": "BASELINE_MISSING",
            "Baseline must be question_status.py --format json output or a run_report.py baseline artifact": "BASELINE_INVALID",
            "Provide one or more query terms.": "QUERY_MISSING",
            "Unknown question slug: q-1 (no page under wiki/questions/)": "QUESTION_UNKNOWN",
            "Unknown request id: req-missing (no record in sources/source-requests.jsonl)": "REQUEST_UNKNOWN",
            "Unknown source id: paper:missing": "SOURCE_UNKNOWN",
            "Missing sibling workspace script: /workspace/scripts/lint.py": "TOOLING_MISSING",
            (
                "Intake total cap exceeded: open questions total would be 3, "
                "limit is 2."
            ): "INTAKE_TOTAL_CAP_EXCEEDED",
            (
                "Intake rate limit exceeded: 1 question(s) in the last hour plus "
                "1 new question(s) exceeds run.max_intake_per_hour 1."
            ): "INTAKE_RATE_LIMITED",
            "Intake field length exceeded: 1 field exceeds the intake byte limit.": "INTAKE_FIELD_TOO_LONG",
            (
                "Intake batch too large: 101 question(s) exceeds "
                "run.max_mcp_intake_batch_questions 100."
            ): "INTAKE_BATCH_TOO_LARGE",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(expected, helper.classify_error_code(message))

    def test_workspace_status_json_failure_uses_shared_health_document(self):
        status = load_script_module("error_envelope_status", "workspace_status.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = self.run_module(status, ["--project-root", tmpdir, "--format", "json"])

        self.assertEqual(2, code)
        self.assertEqual("", stderr)
        document = json.loads(stdout)
        self.assertEqual("1.0", document["schema_version"])
        self.assertEqual("invalid", document["workspace_health"]["status"])
        self.assertFalse(document["workspace_health"]["materially_valid"])
        self.assertIn("WORKSPACE_REQUIRED_FILE_MISSING", document["workspace_health"]["finding_codes"])
        self.assertEqual("attention_required", document["readiness"]["verdict"])
        self.assertTrue(
            any(
                finding.get("artifacts") == ["research.yml"] and finding.get("remediation")
                for finding in document["workspace_health"]["findings"]
            )
        )

    def test_question_claim_json_conflict_uses_error_envelope_with_details(self):
        claim = load_script_module("error_envelope_question_claim", "question_claim.py")
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            code, _, stderr = self.run_module(
                claim,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "which-benchmarks",
                    "--agent-id",
                    "agent-a",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code, stderr)

            code, stdout, stderr = self.run_module(
                claim,
                [
                    "--project-root",
                    str(target),
                    "claim",
                    "--slug",
                    "which-benchmarks",
                    "--agent-id",
                    "agent-b",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(3, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("CLAIM_HELD", envelope["error_code"])
        self.assertFalse(envelope["recoverable"])
        self.assertIn("Use claim --steal --if-older-than", envelope["remediation"])
        self.assertEqual(
            {"action": "claim", "slug": "which-benchmarks", "agent_id": "agent-b"},
            envelope["details"],
        )

    def test_json_output_scripts_table_documents_required_scripts(self):
        documented = documented_json_output_scripts()

        self.assertEqual(sorted(set(documented)), sorted(documented), "JSON Output Scripts table has duplicates")
        missing = sorted(set(JSON_MODE_SCRIPTS) - set(documented))
        self.assertEqual([], missing, "document every required JSON-mode script in orchestrator-handoff.md")

    def test_json_output_scripts_table_uses_stable_error_codes(self):
        missing = sorted(documented_json_output_error_codes() - documented_stable_error_codes())

        self.assertEqual([], missing, "document every JSON Output Scripts table code in Stable error codes")

    def test_intake_limit_error_codes_are_documented(self):
        rows = dict(documented_json_output_script_rows())
        intake_codes = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", rows["intake_questions.py"]))

        self.assertIn("INTAKE_TOTAL_CAP_EXCEEDED", intake_codes)
        self.assertIn("INTAKE_RATE_LIMITED", intake_codes)
        self.assertIn("INTAKE_FIELD_TOO_LONG", intake_codes)
        stable_codes = documented_stable_error_codes()
        self.assertIn("INTAKE_TOTAL_CAP_EXCEEDED", stable_codes)
        self.assertIn("INTAKE_RATE_LIMITED", stable_codes)
        self.assertIn("INTAKE_FIELD_TOO_LONG", stable_codes)
        self.assertIn("INTAKE_BATCH_TOO_LARGE", stable_codes)

    def test_documented_json_mode_scripts_use_shared_error_helper(self):
        for name in documented_json_output_scripts():
            with self.subTest(script=name):
                path = SCRIPTS / name
                self.assertTrue(path.is_file(), f"documented JSON-mode script is missing: {name}")
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                self.assertTrue(script_imports_error_helper(tree), f"{name} must import _script_errors")
                self.assertTrue(
                    script_calls_error_helper(tree),
                    f"{name} must call handle_system_exit, emit_error, or error_envelope",
                )


if __name__ == "__main__":
    unittest.main()
