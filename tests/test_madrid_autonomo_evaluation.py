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
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "madrid-autonomo-workspace"
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"missing workspace script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def copy_fixture(root: Path) -> Path:
    target = root / "workspace"
    shutil.copytree(FIXTURE, target, ignore=shutil.ignore_patterns("reports"))
    return target


class MadridAutonomoEvaluationTests(unittest.TestCase):
    def run_module(self, module, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_fixture_reaches_blocked_on_sources_from_artifacts(self):
        status_module = load_script_module("madrid_status", SCRIPTS / "workspace_status.py")
        readiness_module = load_script_module("madrid_readiness", SCRIPTS / "publication_readiness.py")
        coverage_module = load_script_module("madrid_coverage", SCRIPTS / "coverage_manifest.py")
        run_report_module = load_script_module("madrid_run_report", SCRIPTS / "run_report.py")
        expected_summary = json.loads((FIXTURE / "reports" / "expected-summary.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = copy_fixture(Path(tmpdir))

            status = status_module.build_status_document(workspace)
            readiness = readiness_module.build_readiness_document(
                workspace,
                embedded_inputs={"status": status, "lint": {"stats": {"issue_counts": {}}, "issues": []}},
            )
            code, coverage_stdout, coverage_stderr = self.run_module(
                coverage_module,
                ["--project-root", str(workspace), "evaluate", "--slug", "autonomo-madrid", "--format", "json"],
            )
            report_code, report_stdout, report_stderr = self.run_module(
                run_report_module,
                [
                    "--project-root",
                    str(workspace),
                    "--run-id",
                    "run-2026-07-04-madrid",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual("blocked_on_sources", status["readiness"]["verdict"])
        self.assertEqual("blocked_on_sources", readiness["verdict"])
        self.assertEqual(
            {
                "req-20260704-current-autonomo-fee",
                "req-20260704-madrid-irpf-scale",
                "req-20260704-pae-due-circe-individual",
            },
            set(status["questions"]["blocked_open_request_ids"]),
        )
        self.assertEqual(1, status["candidates"]["official_candidates"])
        self.assertEqual(1, status["candidates"]["aggregator_candidates"])
        self.assertEqual(1, status["candidates"]["linked_to_source_requests"])
        self.assertEqual(0, code, coverage_stderr)
        coverage_payload = json.loads(coverage_stdout)
        self.assertEqual("blocked", coverage_payload["coverage_verdict"])
        self.assertEqual(0, report_code, report_stderr)
        report = json.loads(report_stdout)
        official = report["official_source_evaluation"]
        self.assertEqual(expected_summary["final_verdict"], official["final_verdict"])
        self.assertEqual(expected_summary["blocked_request_ids"], official["blocked_request_ids"])
        self.assertEqual(expected_summary["candidate_total"], official["candidate_summary"]["total"])
        self.assertEqual(expected_summary["official_candidates"], official["candidate_summary"]["official_candidates"])
        self.assertEqual(expected_summary["aggregator_candidates"], official["candidate_summary"]["aggregator_candidates"])

    def test_missing_blocking_request_link_changes_verdict_to_attention_required(self):
        status_module = load_script_module("madrid_status_attention", SCRIPTS / "workspace_status.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = copy_fixture(Path(tmpdir))
            question_path = workspace / "wiki" / "questions" / "current-fee.md"
            text = question_path.read_text(encoding="utf-8")
            frontmatter_text, body = text.split("---\n", 2)[1:]
            frontmatter = yaml.safe_load(frontmatter_text)
            frontmatter["blocking_request_ids"] = []
            question_path.write_text(
                "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n" + body,
                encoding="utf-8",
            )
            requests_path = workspace / "sources" / "source-requests.jsonl"
            records = [
                json.loads(line)
                for line in requests_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            for record in records:
                if record.get("request_id") == "req-20260704-current-autonomo-fee":
                    record["question_slugs"] = []
            requests_path.write_text(
                "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
                encoding="utf-8",
            )

            status = status_module.build_status_document(workspace)

        self.assertEqual("attention_required", status["readiness"]["verdict"])
        self.assertIn("current-fee", status["questions"]["blocked_slugs_missing_requests"])
        reason_codes = {reason["code"] for reason in status["readiness"]["verdict_reasons"]}
        self.assertIn("blocked_request_link_missing", reason_codes)

    def test_legacy_web_sidecar_name_produces_inventory_finding(self):
        inventory_module = load_script_module("madrid_inventory", SCRIPTS / "source_inventory.py")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = copy_fixture(Path(tmpdir))
            canonical = workspace / "raw" / "web" / "seg-social-fee.html.provenance.yml"
            legacy = workspace / "raw" / "web" / "seg-social-fee.provenance.yml"
            canonical.rename(legacy)

            code, stdout, stderr = self.run_module(
                inventory_module,
                [
                    "--project-root",
                    str(workspace),
                    "--dry-run",
                    "--report",
                    "--format",
                    "json",
                ],
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        warning_text = "\n".join(report["warnings"])
        self.assertIn("legacy web provenance sidecar missing .html segment", warning_text)
        self.assertIn("raw/web/seg-social-fee.html.provenance.yml", warning_text)


if __name__ == "__main__":
    unittest.main()
