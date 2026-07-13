import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SURFACES = {
    "doctor": (SCRIPTS / "doctor.py", 1),
    "smoke": (SCRIPTS / "smoke_validate_workspace.py", 1),
    "status": (SCRIPTS / "workspace_status.py", 2),
    "lint": (SCRIPTS / "lint.py", 2),
    "readiness": (SCRIPTS / "publication_readiness.py", 2),
}


class WorkspaceHealthContractTests(unittest.TestCase):
    def run_surface(self, script: Path, project_root: Path, output_format: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "--project-root",
                str(project_root),
                "--format",
                output_format,
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )

    def test_all_workspace_surfaces_share_material_finding_codes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            code_sets: dict[str, set[str]] = {}

            for name, (script, expected_exit) in SURFACES.items():
                result = self.run_surface(script, project_root, "json")
                self.assertEqual(expected_exit, result.returncode, (name, result.stdout, result.stderr))
                payload = json.loads(result.stdout)
                health = payload["workspace_health"]
                self.assertEqual("invalid", health["status"])
                self.assertFalse(health["materially_valid"])
                code_sets[name] = set(health["finding_codes"])

            expected = code_sets["doctor"]
            self.assertIn("WORKSPACE_REQUIRED_FILE_MISSING", expected)
            self.assertIn("WORKSPACE_REQUIRED_DIRECTORY_MISSING", expected)
            for name, actual in code_sets.items():
                self.assertEqual(expected, actual, name)
            self.assertEqual([], list(project_root.iterdir()), "read-only health checks must not mutate an invalid root")

    def test_text_surfaces_expose_the_same_stable_codes_as_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            for name in ("doctor", "smoke", "status", "lint"):
                script, _ = SURFACES[name]
                json_result = self.run_surface(script, project_root, "json")
                codes = json.loads(json_result.stdout)["workspace_health"]["finding_codes"]
                text_result = self.run_surface(script, project_root, "text")
                for code in codes:
                    self.assertIn(code, text_result.stdout, (name, code, text_result.stdout, text_result.stderr))


if __name__ == "__main__":
    unittest.main()
