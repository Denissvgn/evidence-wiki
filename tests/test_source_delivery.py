import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
ARXIV_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "arxiv-source-project"
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SOURCE_DELIVERY_DOC = REPO_ROOT / "workspace-template" / "docs" / "source-delivery.md"
SOURCE_DISCOVERY_DOC = REPO_ROOT / "workspace-template" / "docs" / "source-discovery.md"
SOURCE_MANIFEST_DOC = REPO_ROOT / "workspace-template" / "docs" / "source-manifest.md"
NORMALIZED_SOURCE_DOC = REPO_ROOT / "workspace-template" / "docs" / "normalized-source-format.md"
SOURCE_DELIVERY_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "source-delivery"


INVENTORY = load_script_module("source_delivery_inventory", "source_inventory.py")
NORMALIZE = load_script_module("source_delivery_normalize", "normalize_sources.py")
TAXONOMY = load_script_module("source_delivery_failure_taxonomy", "source_failure_taxonomy.py")


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["script", *args]
    try:
        yield
    finally:
        sys.argv = old


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(workspace: Path) -> list[dict]:
    manifest = workspace / "sources" / "manifest.jsonl"
    return [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]


def load_frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n")
    block = text[4 : text.find("\n---", 4)]
    return yaml.safe_load(block)


class SourceDeliveryTests(unittest.TestCase):
    """E17-T01/T02: delivery idempotency and provenance sidecar handling."""

    def copy_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        shutil.copytree(ARXIV_FIXTURE, workspace)
        return workspace

    def run_inventory_capture(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = INVENTORY.main(["--project-root", str(workspace), *args])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def run_inventory_main(self, workspace: Path, *args: str) -> tuple[int, str]:
        code, _, stderr = self.run_inventory_capture(workspace, *args)
        return code, stderr

    def run_normalize_capture(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(["--project-root", str(workspace), *args])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def inventory_workspace(self, workspace: Path) -> None:
        code, _, stderr = self.run_inventory_capture(workspace)
        self.assertEqual(0, code, stderr)

    def force_pdf_fallback_manifest(self, workspace: Path, source_id: str = "paper:2601.00001v1") -> None:
        manifest = workspace / "sources" / "manifest.jsonl"
        records = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        for record in records:
            if record.get("id") == source_id:
                record.pop("latex_root", None)
                record.pop("entrypoint", None)
                break
        else:
            raise AssertionError(f"manifest has no record {source_id!r}")
        manifest.write_text(
            "\n".join(json.dumps(record, sort_keys=True, separators=(",", ":")) for record in records) + "\n"
        )

    def build_records(self, workspace: Path) -> tuple[list[dict], list[str]]:
        config = INVENTORY.load_config(workspace)
        records, warnings, _ = INVENTORY.build_records(workspace, config, previous_detected_at={})
        return records, warnings

    def add_raw_web_root(self, workspace: Path) -> None:
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        roots = config.setdefault("raw", {}).setdefault("source_roots", [])
        if "raw/web" not in roots:
            roots.append("raw/web")
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def record_by_id(self, records: list[dict], source_id: str) -> dict:
        matches = [record for record in records if record.get("id") == source_id]
        self.assertEqual(1, len(matches), f"expected exactly one record {source_id}")
        return matches[0]

    def write_sidecar(self, workspace: Path, target_rel: str, data: dict) -> Path:
        sidecar = workspace / f"{target_rel}.provenance.yml"
        sidecar.write_text(yaml.safe_dump(data, sort_keys=False))
        return sidecar

    def test_incremental_delivery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            code, _ = self.run_inventory_main(workspace)
            self.assertEqual(0, code)
            first = read_manifest(workspace)
            first_ids = sorted(record["id"] for record in first)
            self.assertIn("paper:2601.00001v1", first_ids)

            # Simulate an earlier run: pin one detected_at to a known old value.
            manifest_path = workspace / "sources" / "manifest.jsonl"
            pinned = []
            for record in first:
                if record["id"] == "paper:2601.00001v1":
                    record["detected_at"] = "2020-01-01T00:00:00Z"
                pinned.append(json.dumps(record, sort_keys=True, separators=(",", ":")))
            manifest_path.write_text("\n".join(pinned) + "\n")

            # Deliver one new file, then re-run inventory.
            (workspace / "raw" / "pdf" / "delivered-report.pdf").write_text("Delivered synthetic report.\n")
            code, _ = self.run_inventory_main(workspace)
            self.assertEqual(0, code)
            second = read_manifest(workspace)
            second_ids = [record["id"] for record in second]

            self.assertEqual(len(second_ids), len(set(second_ids)), "manifest must not contain duplicate ids")
            for source_id in first_ids:
                self.assertIn(source_id, second_ids, "prior records must survive incremental delivery")
            self.assertEqual(len(first_ids) + 1, len(second_ids))
            paper = self.record_by_id(second, "paper:2601.00001v1")
            self.assertEqual("2020-01-01T00:00:00Z", paper["detected_at"])

    def test_json_missing_config_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing_workspace = Path(tmpdir) / "missing"

            code, stdout, stderr = self.run_inventory_capture(
                missing_workspace,
                "--format",
                "json",
                "--report",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertEqual("CONFIG_MISSING", envelope["error_code"])
        self.assertIn("research.yml", envelope["message"])
        self.assertIn("--project-root", envelope["remediation"])

    def test_json_invalid_manifest_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            manifest = workspace / "sources" / "manifest.jsonl"
            manifest.write_text("{not json}\n")

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("MANIFEST_INVALID", envelope["error_code"])
        self.assertIn("Invalid JSONL", envelope["message"])

    def test_json_report_shape_for_arxiv_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual("source_inventory_report", report["document_type"])
        self.assertFalse(report["dry_run"])
        self.assertEqual("sources/manifest.jsonl", report["manifest"])
        self.assertEqual(3, report["total"])
        self.assertEqual({"paper": 1, "repo_link": 1, "web_link": 1}, report["kind_counts"])
        self.assertEqual(
            {"paired": 1, "pdf_only": 0, "latex_only": 0, "ambiguous": 0},
            report["pairing_counts"],
        )
        self.assertEqual("ready_for_normalization", report["readiness"])
        self.assertIn("Proceed to source normalization.", report["next_actions"])
        self.assertIn("summary paired=1", stderr)

    def test_json_report_includes_malformed_sidecar_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            sidecar = workspace / "raw" / "pdf" / "2601.00001v1.pdf.provenance.yml"
            sidecar.write_text("{[ this is not yaml\n")

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["dry_run"])
        self.assertTrue(any("malformed provenance sidecar" in warning for warning in report["warnings"]))
        self.assertIn("warning:", stderr)

    def test_json_reject_mismatch_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "retrieved_by": "fetch-agent/arxiv",
                    "checksum": "sha256:" + "0" * 64,
                },
            )

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--reject-mismatch",
            )

        self.assertEqual(1, code)
        report = json.loads(stdout)
        self.assertTrue(report["dry_run"])
        self.assertTrue(any("strict checksum refusal" in warning for warning in report["warnings"]))
        envelope = json.loads(stderr)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertEqual("INVENTORY_CHECKSUM_MISMATCH", envelope["error_code"])
        self.assertIn("strict checksum", envelope["message"])
        self.assertEqual(["paper:2601.00001v1"], envelope["details"]["source_ids"])

    def test_json_dry_run_without_report_emits_jsonl_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--dry-run",
            )

        self.assertEqual(0, code, stderr)
        lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        self.assertEqual(3, len(lines))
        self.assertEqual(["link:example-org-fixture-project-e51812dad4", "link:github-example-fixture-repo-5f629f49ca", "paper:2601.00001v1"], [line["id"] for line in lines])
        self.assertNotIn("[", stdout[:1])
        self.assertIn("would write 3 records", stderr)

    def test_text_report_output_remains_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "text",
                "--report",
                "--dry-run",
            )

        self.assertEqual(0, code, stderr)
        self.assertTrue(stdout.startswith("# Source Inventory Report\n"))
        self.assertIn("## Counts by Kind", stdout)
        self.assertIn("- Manifest: `sources/manifest.jsonl`", stdout)
        self.assertIn("would write 3 records", stderr)

    def test_normalize_json_missing_manifest_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            (workspace / "sources" / "manifest.jsonl").unlink()

            code, stdout, stderr = self.run_normalize_capture(workspace, "--format", "json")

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertEqual("MANIFEST_MISSING", envelope["error_code"])
        self.assertIn("Missing manifest", envelope["message"])

    def test_normalize_json_missing_poppler_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)
            self.force_pdf_fallback_manifest(workspace)

            with mock.patch.object(NORMALIZE.shutil, "which", return_value=None):
                code, stdout, stderr = self.run_normalize_capture(
                    workspace,
                    "--source-id",
                    "paper:2601.00001v1",
                    "--pdf-extractor",
                    "poppler",
                    "--format",
                    "json",
                )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("DEPENDENCY_MISSING", envelope["error_code"])
        self.assertIn("pdftotext", envelope["message"])

    def test_normalize_json_missing_pypdf_uses_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)
            self.force_pdf_fallback_manifest(workspace)

            with mock.patch.object(NORMALIZE.importlib.util, "find_spec", return_value=None):
                code, stdout, stderr = self.run_normalize_capture(
                    workspace,
                    "--source-id",
                    "paper:2601.00001v1",
                    "--format",
                    "json",
                )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("DEPENDENCY_MISSING", envelope["error_code"])
        self.assertIn("pypdf", envelope["message"])

    def test_normalize_dry_run_json_report_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)

            code, stdout, stderr = self.run_normalize_capture(
                workspace,
                "--all",
                "--dry-run",
                "--format",
                "json",
            )

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual("source_normalization_report", report["document_type"])
        self.assertTrue(report["dry_run"])
        self.assertEqual("sources/manifest.jsonl", report["manifest"])
        self.assertEqual("sources/normalized", report["normalized_dir"])
        self.assertEqual("all", report["selector"])
        self.assertEqual(3, report["summary"]["selected"])
        self.assertEqual(3, report["summary"]["planned"])
        self.assertEqual(3, report["summary"]["dry_run"])
        self.assertEqual(3, report["summary"]["would_create"])
        self.assertEqual(0, report["summary"]["failed"])
        self.assertEqual(
            {"latex": 1, "pdf": 0, "links": 2, "html": 0, "tables": 0, "codebase": 0, "adapter": 0},
            report["summary"]["methods"],
        )
        self.assertEqual(3, len(report["actions"]))
        self.assertEqual({"would_create"}, {action["action"] for action in report["actions"]})

    def test_normalize_all_json_report_lists_created_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)

            code, stdout, stderr = self.run_normalize_capture(workspace, "--all", "--format", "json")

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertFalse(report["dry_run"])
        self.assertEqual(3, report["summary"]["created"])
        self.assertEqual(0, report["summary"]["updated"])
        self.assertEqual(0, report["summary"]["failed"])
        self.assertEqual(3, len(report["actions"]))
        self.assertEqual({"created"}, {action["action"] for action in report["actions"]})
        for action in report["actions"]:
            self.assertIn("source_id", action)
            self.assertIn("method", action)
            self.assertTrue(action["output"].startswith("sources/normalized/"))
            self.assertIn(action["status"], {"content_extracted", "stubbed"})
            self.assertIs(action["stale"], False)
            self.assertIn("warnings", action)

    def test_normalize_json_keeps_partial_pdf_extraction_nonfatal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)
            self.force_pdf_fallback_manifest(workspace)

            with mock.patch.object(NORMALIZE.shutil, "which", return_value="pdftotext"):
                with mock.patch.object(
                    NORMALIZE,
                    "extract_pdf_text",
                    return_value=("", ["raw/pdf/2601.00001v1.pdf: pdftotext produced no output"], True),
                ):
                    code, stdout, stderr = self.run_normalize_capture(
                        workspace,
                        "--source-id",
                        "paper:2601.00001v1",
                        "--pdf-extractor",
                        "poppler",
                        "--format",
                        "json",
                    )

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        report = json.loads(stdout)
        self.assertEqual(1, report["summary"]["partial"])
        self.assertEqual(0, report["summary"]["failed"])
        self.assertEqual(1, len(report["actions"]))
        action = report["actions"][0]
        self.assertEqual("paper:2601.00001v1", action["source_id"])
        self.assertEqual("partial", action["status"])
        self.assertEqual("created", action["action"])
        self.assertTrue(any("needs OCR" in warning for warning in action["warnings"]))

    def test_normalize_text_output_remains_stderr_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.inventory_workspace(workspace)

            code, stdout, stderr = self.run_normalize_capture(workspace, "--all", "--format", "text")

        self.assertEqual(0, code, stderr)
        self.assertEqual("", stdout)
        self.assertIn("created sources/normalized/paper--2601.00001v1.md", stderr)
        self.assertIn("summary selector=all selected=3 planned=3", stderr)

    def test_sidecar_merges_provenance_with_verified_checksum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = "raw/pdf/2601.00001v1.pdf"
            self.write_sidecar(
                workspace,
                pdf_rel,
                {
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-06-10T12:00:00Z",
                    "retrieved_by": "fetch-agent/arxiv",
                    "checksum": sha256_of(workspace / pdf_rel),
                    "request_id": "req-1a2b3c4d5e",
                },
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            provenance = paper["provenance"]
            self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["origin_url"])
            self.assertEqual("CC-BY-4.0", provenance["license"])
            self.assertEqual("2026-06-10T12:00:00Z", provenance["retrieved_at"])
            self.assertEqual("fetch-agent/arxiv", provenance["retrieved_by"])
            self.assertEqual("req-1a2b3c4d5e", provenance["request_id"])
            self.assertTrue(provenance["checksum_verified"])
            self.assertEqual(f"{pdf_rel}.provenance.yml", provenance["sidecar_path"])
            self.assertFalse([warning for warning in warnings if "provenance" in warning])

    def test_paired_paper_carries_provenance_for_both_captures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            bundle_rel = "raw/other/arXiv-2601.00001v1"
            pdf_rel = "raw/pdf/2601.00001v1.pdf"
            self.write_sidecar(
                workspace,
                bundle_rel,
                {
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-06-10T12:00:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "request_id": "req-1a2b3c4d5e",
                    "candidate_id": "cand-arxiv-bundle",
                },
            )
            self.write_sidecar(
                workspace,
                pdf_rel,
                {
                    "origin_url": "https://arxiv.org/pdf/2601.00001v1",
                    "license": "CC-BY-4.0",
                    "retrieved_at": "2026-06-10T12:01:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "checksum": sha256_of(workspace / pdf_rel),
                    "request_id": "req-1a2b3c4d5e",
                    "candidate_id": "cand-arxiv-pdf",
                },
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            provenance = paper["provenance"]
            self.assertEqual(f"{bundle_rel}.provenance.yml", provenance["sidecar_path"])
            self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["origin_url"])
            self.assertEqual("2026-06-10T12:00:00Z", provenance["retrieved_at"])
            self.assertEqual("req-1a2b3c4d5e", provenance["request_id"])
            self.assertEqual("cand-arxiv-bundle", provenance["candidate_id"])
            self.assertNotIn("checksum", provenance)
            self.assertNotIn("checksum_verified", provenance)

            additional = paper["additional_provenance"]
            self.assertEqual(1, len(additional))
            paired = additional[0]
            self.assertEqual(pdf_rel, paired["path"])
            self.assertEqual(f"{pdf_rel}.provenance.yml", paired["sidecar_path"])
            self.assertEqual("https://arxiv.org/pdf/2601.00001v1", paired["origin_url"])
            self.assertEqual("2026-06-10T12:01:00Z", paired["retrieved_at"])
            self.assertEqual("CC-BY-4.0", paired["license"])
            self.assertEqual(sha256_of(workspace / pdf_rel), paired["checksum"])
            self.assertTrue(paired["checksum_verified"])
            self.assertNotIn("request_id", paired)
            self.assertNotIn("candidate_id", paired)
            self.assertFalse([warning for warning in warnings if "provenance" in warning])

    def test_paired_capture_checksum_is_verified_against_its_own_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/other/arXiv-2601.00001v1",
                {"origin_url": "https://arxiv.org/abs/2601.00001v1"},
            )
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {"retrieved_by": "fetch_sources.py/arxiv", "checksum": "sha256:" + "0" * 64},
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            paired = paper["additional_provenance"][0]
            self.assertEqual("raw/pdf/2601.00001v1.pdf", paired["path"])
            self.assertFalse(paired["checksum_verified"])
            self.assertTrue(paper["metadata"]["review_required"])
            self.assertTrue(
                any("checksum mismatch for raw/pdf/2601.00001v1.pdf" in warning for warning in warnings)
            )

    def write_paired_sidecars(self, workspace: Path, pdf_checksum: str) -> str:
        """Bundle sidecar without a checksum plus a PDF sidecar carrying `pdf_checksum`."""
        pdf_rel = "raw/pdf/2601.00001v1.pdf"
        self.write_sidecar(
            workspace,
            "raw/other/arXiv-2601.00001v1",
            {"origin_url": "https://arxiv.org/abs/2601.00001v1"},
        )
        self.write_sidecar(
            workspace,
            pdf_rel,
            {"retrieved_by": "fetch_sources.py/arxiv", "checksum": pdf_checksum},
        )
        return pdf_rel

    def test_reject_mismatch_refuses_the_record_whose_secondary_capture_did_not_verify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = self.write_paired_sidecars(workspace, "sha256:" + "0" * 64)

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--reject-mismatch",
            )

        self.assertEqual(1, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(2, report["total"], report)
        self.assertEqual(
            {"paired": 0, "pdf_only": 0, "latex_only": 0, "ambiguous": 0},
            report["pairing_counts"],
            report,
        )
        self.assertIn(
            f"strict checksum refusal: paper:2601.00001v1 capture {pdf_rel} "
            "checksum is present but not verified",
            report["warnings"],
            report,
        )

        envelope = json.loads(stderr)
        self.assertEqual("INVENTORY_CHECKSUM_MISMATCH", envelope["error_code"], envelope)
        self.assertEqual(["paper:2601.00001v1"], envelope["details"]["source_ids"], envelope)
        refusals = envelope["details"]["refusals"]
        self.assertEqual(1, len(refusals), envelope)
        self.assertEqual("paper:2601.00001v1", refusals[0]["source_id"], envelope)
        self.assertEqual("checksum_mismatch", refusals[0]["reason"], envelope)
        self.assertEqual(pdf_rel, refusals[0]["path"], envelope)
        self.assertIn(pdf_rel, refusals[0]["message"], envelope)

    def test_reject_mismatch_admits_the_record_whose_secondary_capture_verified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = "raw/pdf/2601.00001v1.pdf"
            self.write_paired_sidecars(workspace, sha256_of(workspace / pdf_rel))

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--reject-mismatch",
            )

        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual(3, report["total"], report)
        self.assertEqual(
            [],
            [warning for warning in report["warnings"] if "strict checksum refusal" in warning],
            report,
        )

    def test_require_checksum_refuses_the_paired_record_for_its_primary_capture_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = self.write_paired_sidecars(workspace, "sha256:" + "0" * 64)

            code, _, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--require-checksum",
            )

        self.assertEqual(1, code, stderr)
        envelope = json.loads(stderr)
        self.assertEqual("INVENTORY_CHECKSUM_REQUIRED", envelope["error_code"], envelope)
        refusals = envelope["details"]["refusals"]
        self.assertEqual(
            [],
            [refusal for refusal in refusals if refusal["reason"] != "checksum_required"],
            envelope,
        )
        self.assertEqual([], [refusal for refusal in refusals if "path" in refusal], envelope)
        self.assertEqual([], [refusal for refusal in refusals if pdf_rel in refusal["message"]], envelope)
        paper_refusals = [refusal for refusal in refusals if refusal["source_id"] == "paper:2601.00001v1"]
        self.assertEqual(1, len(paper_refusals), envelope)

    def write_two_link_captures(self, workspace: Path) -> tuple[str, str]:
        """Two link files naming one URL: the first verifies, the second does not.

        Inventory folds both into a single link record, so the record's primary capture
        is a regular file that verifies while its secondary capture is a proven mismatch.
        """
        verified_rel = "raw/links/capture-a.txt"
        mismatched_rel = "raw/links/capture-b.txt"
        for rel in (verified_rel, mismatched_rel):
            (workspace / rel).write_text("https://github.com/example/fixture-repo\n")
        self.write_sidecar(workspace, verified_rel, {"checksum": sha256_of(workspace / verified_rel)})
        self.write_sidecar(workspace, mismatched_rel, {"checksum": "sha256:" + "0" * 64})
        return verified_rel, mismatched_rel

    def test_reject_mismatch_refuses_a_link_record_whose_second_link_file_did_not_verify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            _, mismatched_rel = self.write_two_link_captures(workspace)

            code, _, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--reject-mismatch",
            )

        self.assertEqual(1, code, stderr)
        envelope = json.loads(stderr)
        self.assertEqual("INVENTORY_CHECKSUM_MISMATCH", envelope["error_code"], envelope)
        self.assertEqual(
            ["link:github-example-fixture-repo-5f629f49ca"], envelope["details"]["source_ids"], envelope
        )
        refusals = envelope["details"]["refusals"]
        self.assertEqual(1, len(refusals), envelope)
        self.assertEqual(mismatched_rel, refusals[0]["path"], envelope)

    def test_require_checksum_admits_a_link_record_whose_second_link_file_did_not_verify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_two_link_captures(workspace)

            code, _, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--require-checksum",
            )

        self.assertEqual(1, code, stderr)
        envelope = json.loads(stderr)
        self.assertEqual("INVENTORY_CHECKSUM_REQUIRED", envelope["error_code"], envelope)
        self.assertNotIn(
            "link:github-example-fixture-repo-5f629f49ca", envelope["details"]["source_ids"], envelope
        )

    def test_both_strict_flags_still_name_the_capture_that_mismatched(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = self.write_paired_sidecars(workspace, "sha256:" + "0" * 64)

            code, _, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
                "--require-checksum",
                "--reject-mismatch",
            )

        self.assertEqual(1, code, stderr)
        envelope = json.loads(stderr)
        self.assertEqual("INVENTORY_CHECKSUM_MISMATCH", envelope["error_code"], envelope)
        paper_refusals = [
            refusal
            for refusal in envelope["details"]["refusals"]
            if refusal["source_id"] == "paper:2601.00001v1"
        ]
        self.assertEqual(
            ["checksum_required", "checksum_mismatch"],
            [refusal["reason"] for refusal in paper_refusals],
            envelope,
        )
        self.assertEqual([pdf_rel], [refusal["path"] for refusal in paper_refusals if "path" in refusal], envelope)
        self.assertEqual(3, len(envelope["details"]["source_ids"]), envelope)
        self.assertIn("refused 3 sources", envelope["message"], envelope)

    def test_a_record_raises_one_refusal_for_each_capture_that_mismatched(self):
        record = {
            "id": "paper:2601.00001v1",
            "provenance": {"checksum": "sha256:" + "a" * 64, "checksum_verified": True},
            "additional_provenance": [
                {"path": "raw/pdf/first.pdf", "checksum": "sha256:" + "0" * 64, "checksum_verified": False},
                {"path": "raw/pdf/second.pdf", "checksum": "sha256:" + "1" * 64, "checksum_verified": False},
            ],
        }

        _, _, refusals = INVENTORY.strict_checksum_refusals(
            [record], require_checksum=False, reject_mismatch=True
        )

        self.assertEqual(
            ["raw/pdf/first.pdf", "raw/pdf/second.pdf"],
            [refusal["path"] for refusal in refusals],
            record,
        )
        self.assertEqual(["paper:2601.00001v1"], INVENTORY.strict_refusal_details(refusals)["source_ids"], record)
        self.assertIn("refused 1 source ", INVENTORY.strict_refusal_message(refusals), record)

    def verified_primary_with_unverified_capture(self) -> dict:
        """A record whose primary capture verified and whose secondary capture did not."""
        return {
            "id": "paper:2601.00001v1",
            "provenance": {"checksum": "sha256:" + "a" * 64, "checksum_verified": True},
            "additional_provenance": [
                {
                    "path": "raw/pdf/2601.00001v1.pdf",
                    "checksum": "sha256:" + "0" * 64,
                    "checksum_verified": False,
                }
            ],
        }

    def test_require_checksum_keeps_a_record_whose_secondary_capture_did_not_verify(self):
        record = self.verified_primary_with_unverified_capture()

        kept, warnings, refusals = INVENTORY.strict_checksum_refusals(
            [record], require_checksum=True, reject_mismatch=False
        )

        self.assertEqual([record], kept, record)
        self.assertEqual([], warnings, record)
        self.assertEqual([], refusals, record)

    def test_reject_mismatch_refuses_a_record_whose_primary_capture_verified(self):
        record = self.verified_primary_with_unverified_capture()

        kept, warnings, refusals = INVENTORY.strict_checksum_refusals(
            [record], require_checksum=False, reject_mismatch=True
        )

        self.assertEqual([], kept, record)
        self.assertEqual(1, len(refusals), record)
        self.assertEqual("raw/pdf/2601.00001v1.pdf", refusals[0]["path"], record)
        self.assertEqual("checksum_mismatch", refusals[0]["reason"], record)
        self.assertEqual([refusals[0]["message"]], warnings, record)

    def test_unmatched_sidecar_still_reports_no_source_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/other/arXiv-2601.00001v1/main.tex",
                {"origin_url": "https://arxiv.org/abs/2601.00001v1"},
            )

            _, warnings = self.build_records(workspace)

            self.assertTrue(any("matches no source record" in warning for warning in warnings))

    def test_sidecar_preserves_curation_metadata_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://official.example/product",
                    "retrieved_at": "2026-07-02T12:00:00Z",
                    "retrieved_by": "fetch-agent/manual-web",
                    "terms_url": "https://official.example/terms",
                    "terms_note": "Official terms page reviewed before capture.",
                    "candidate_id": "cand-official-product",
                },
            )

            records, warnings = self.build_records(workspace)
            provenance = self.record_by_id(records, "paper:2601.00001v1")["provenance"]

        self.assertEqual("https://official.example/terms", provenance["terms_url"])
        self.assertEqual("Official terms page reviewed before capture.", provenance["terms_note"])
        self.assertEqual("cand-official-product", provenance["candidate_id"])
        self.assertFalse(any("unknown provenance field ignored: terms_url" in warning for warning in warnings))
        self.assertFalse(any("unknown provenance field ignored: terms_note" in warning for warning in warnings))
        self.assertFalse(any("unknown provenance field ignored: candidate_id" in warning for warning in warnings))

    def test_currentness_sidecar_fields_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_at": "2026-06-10T12:00:00Z",
                    "effective_date": "2026-01-01",
                    "publication_date": "2026-01-15T00:00:00Z",
                    "validity_period": "2026-01-01/2026-12-31",
                    "date_not_available": "No separate effective date appears on the source page.",
                    "source_status": "available",
                    "surprise": "field",
                },
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            provenance = paper["provenance"]
            self.assertEqual("2026-01-01", provenance["effective_date"])
            self.assertEqual("2026-01-15T00:00:00Z", provenance["publication_date"])
            self.assertEqual("2026-01-01/2026-12-31", provenance["validity_period"])
            self.assertEqual(
                "No separate effective date appears on the source page.",
                provenance["date_not_available"],
            )
            self.assertEqual("available", provenance["source_status"])
            self.assertTrue(any("unknown provenance field ignored: surprise" in warning for warning in warnings))
            self.assertFalse(any("effective_date" in warning for warning in warnings))
            self.assertFalse(any("publication_date" in warning for warning in warnings))
            self.assertFalse(any("validity_period" in warning for warning in warnings))
            self.assertFalse(any("date_not_available" in warning for warning in warnings))
            self.assertFalse(any("source_status" in warning for warning in warnings))

    def test_canonical_web_sidecar_preserves_official_metadata_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.add_raw_web_root(workspace)
            raw_rel = "raw/web/canonical-official-web.html"
            raw_path = workspace / raw_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("<html><body>Official current legal figure.</body></html>\n", encoding="utf-8")
            expected_sha = sha256_of(raw_path)
            self.write_sidecar(
                workspace,
                raw_rel,
                {
                    "url": "https://seg-social.es/official/current-fee",
                    "retrieved_at": "2026-07-04T12:00:00Z",
                    "sha256": expected_sha,
                    "source_type": "official_web",
                    "jurisdiction": "ES",
                    "publisher": "Seguridad Social",
                    "date_metadata": {
                        "effective_date": "2026-01-01",
                        "valid_for_year": 2026,
                    },
                    "supported_evidence_areas": ["social_security_contributions", "current_legal_figure"],
                    "curation_notes": "Official page captured for the Madrid regression fixture.",
                },
            )

            records, warnings = self.build_records(workspace)
            record = self.record_by_id(records, INVENTORY.stable_id(raw_rel))
            provenance = record["provenance"]

        self.assertEqual("https://seg-social.es/official/current-fee", provenance["url"])
        self.assertEqual("https://seg-social.es/official/current-fee", provenance["origin_url"])
        self.assertEqual(expected_sha, provenance["sha256"])
        self.assertEqual(expected_sha, provenance["checksum"])
        self.assertTrue(provenance["checksum_verified"])
        self.assertEqual("official_web", provenance["source_type"])
        self.assertEqual("ES", provenance["jurisdiction"])
        self.assertEqual("Seguridad Social", provenance["publisher"])
        self.assertEqual({"effective_date": "2026-01-01", "valid_for_year": 2026}, provenance["date_metadata"])
        self.assertEqual(
            ["social_security_contributions", "current_legal_figure"],
            provenance["supported_evidence_areas"],
        )
        self.assertEqual("Official page captured for the Madrid regression fixture.", provenance["curation_notes"])
        self.assertFalse(any("unknown provenance field ignored" in warning for warning in warnings))

    def test_sidecar_preserves_complete_evidence_usability_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.add_raw_web_root(workspace)
            raw_rel = "raw/web/official-guidance.html"
            raw_path = workspace / raw_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("<html><body>Official guidance fixture.</body></html>\n", encoding="utf-8")
            self.write_sidecar(
                workspace,
                raw_rel,
                {
                    "url": "https://official.example/guidance",
                    "retrieved_at": "2026-07-04T12:00:00Z",
                    "sha256": sha256_of(raw_path),
                    "evidence_usability_override": {
                        "usable": True,
                        "reviewed_by": "verifier-agent",
                        "reviewed_at": "2026-07-04T12:30:00Z",
                        "reason": "Rich official guidance capture; JavaScript warning is boilerplate.",
                    },
                },
            )

            records, warnings = self.build_records(workspace)
            record = self.record_by_id(records, INVENTORY.stable_id(raw_rel))

        self.assertEqual(
            {
                "usable": True,
                "reviewed_by": "verifier-agent",
                "reviewed_at": "2026-07-04T12:30:00Z",
                "reason": "Rich official guidance capture; JavaScript warning is boilerplate.",
            },
            record["provenance"]["evidence_usability_override"],
        )
        self.assertFalse(any("evidence_usability_override" in warning for warning in warnings), warnings)

    def test_sidecar_rejects_invalid_evidence_usability_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://official.example/guidance",
                    "evidence_usability_override": {
                        "usable": True,
                        "reviewed_by": "",
                        "reviewed_at": "2026-07-04T12:30:00Z",
                        "reason": " ",
                    },
                },
            )

            records, warnings = self.build_records(workspace)
            provenance = self.record_by_id(records, "paper:2601.00001v1")["provenance"]

        self.assertNotIn("evidence_usability_override", provenance)
        self.assertTrue(
            any("reviewed_by" in warning and "reason" in warning for warning in warnings),
            warnings,
        )

    def test_legacy_web_sidecar_name_is_reported_without_merging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.add_raw_web_root(workspace)
            raw_rel = "raw/web/legacy-official-web.html"
            raw_path = workspace / raw_rel
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text("<html><body>Legacy sidecar fixture.</body></html>\n", encoding="utf-8")
            legacy_sidecar = workspace / "raw" / "web" / "legacy-official-web.provenance.yml"
            legacy_sidecar.write_text(
                yaml.safe_dump(
                    {
                        "url": "https://seg-social.es/legacy",
                        "retrieved_at": "2026-07-04T12:00:00Z",
                        "sha256": sha256_of(raw_path),
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            records, warnings = self.build_records(workspace)
            record = self.record_by_id(records, INVENTORY.stable_id(raw_rel))

        self.assertNotIn("provenance", record)
        self.assertTrue(
            any(
                "legacy web provenance sidecar" in warning
                and "raw/web/legacy-official-web.html.provenance.yml" in warning
                for warning in warnings
            )
        )
        self.assertTrue(any("missing canonical provenance sidecar" in warning for warning in warnings))

    def test_delivery_failure_sidecar_fields_are_preserved_and_validated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://www.nvidia.com/en-us/products/workstations/dgx-spark/",
                    "retrieved_at": "2026-07-02T12:00:00Z",
                    "source_status": "unavailable",
                    "delivery_failure_code": "javascript_required",
                    "delivery_failure_detail": "Static fetch returned only the product-page JavaScript shell.",
                    "delivery_failure_remediation": TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS[
                        "javascript_required"
                    ],
                },
            )

            records, warnings = self.build_records(workspace)
            provenance = self.record_by_id(records, "paper:2601.00001v1")["provenance"]

            self.assertEqual("unavailable", provenance["source_status"])
            self.assertEqual("javascript_required", provenance["delivery_failure_code"])
            self.assertIn("JavaScript shell", provenance["delivery_failure_detail"])
            self.assertIn("manual", provenance["delivery_failure_remediation"].lower())
            self.assertFalse(any("delivery_failure_code" in warning for warning in warnings))
            self.assertFalse(any("source_status" in warning for warning in warnings))

            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://official.example/page",
                    "source_status": "gone",
                    "delivery_failure_code": "legal_only_failure",
                    "delivery_failure_detail": "This invalid code should not enter the manifest.",
                },
            )

            records, warnings = self.build_records(workspace)
            provenance = self.record_by_id(records, "paper:2601.00001v1")["provenance"]

            self.assertNotIn("source_status", provenance)
            self.assertNotIn("delivery_failure_code", provenance)
            self.assertEqual(
                "This invalid code should not enter the manifest.",
                provenance["delivery_failure_detail"],
            )
            self.assertTrue(any("provenance source_status must be one of" in warning for warning in warnings))
            self.assertTrue(
                any("provenance delivery_failure_code must be one of" in warning for warning in warnings)
            )

    def test_failure_sidecars_mark_records_unusable_and_report_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            cases = [
                (
                    "raw/other/government-official-error-page.html",
                    "official_error_page",
                    "error_page",
                    "source_status:error_page",
                    "delivery_failure_code:official_error_page",
                ),
                (
                    "raw/other/tls-verification-failed.html",
                    "tls_verification_failed",
                    "unavailable",
                    "source_status:unavailable",
                    "delivery_failure_code:tls_verification_failed",
                ),
                (
                    "raw/other/not-found.html",
                    "not_found",
                    "not_found",
                    "source_status:not_found",
                    "delivery_failure_code:not_found",
                ),
            ]
            for raw_path, code, status, *_ in cases:
                (workspace / raw_path).write_text("<html><body>official delivery failure fixture</body></html>\n")
                self.write_sidecar(
                    workspace,
                    raw_path,
                    {
                        "origin_url": f"https://official.example/{Path(raw_path).stem}",
                        "retrieved_at": "2026-07-02T12:00:00Z",
                        "retrieved_by": "fetch-agent/manual",
                        "source_status": status,
                        "delivery_failure_code": code,
                        "delivery_failure_detail": f"Fixture delivery failure: {code}.",
                        "delivery_failure_remediation": TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS[code],
                    },
                )

            records, warnings = self.build_records(workspace)

            self.assertFalse(any("delivery_failure_code" in warning for warning in warnings))
            for raw_path, _code, _status, status_reason, code_reason in cases:
                source_id = INVENTORY.stable_id(raw_path)
                record = self.record_by_id(records, source_id)
                self.assertFalse(record["evidence_usable"])
                self.assertIn(status_reason, record["unusable_evidence_reasons"])
                self.assertIn(code_reason, record["unusable_evidence_reasons"])

            code, stdout, stderr = self.run_inventory_capture(
                workspace,
                "--format",
                "json",
                "--report",
                "--dry-run",
            )
            self.assertEqual(0, code, stderr)
            report = json.loads(stdout)
            self.assertGreaterEqual(report["evidence_usable_counts"]["unusable"], 3)
            unusable_ids = {record["id"] for record in report["unusable_records"]}
            for raw_path, *_ in cases:
                self.assertIn(INVENTORY.stable_id(raw_path), unusable_ids)

    def test_delivery_failure_taxonomy_is_documented_and_fixture_backed(self):
        expected_codes = {
            "tls_verification_failed",
            "http_error",
            "javascript_required",
            "official_error_page",
            "not_found",
            "content_too_sparse",
            "license_or_terms_unknown",
            "robots_or_terms_blocked",
            "manual_review_required",
        }
        self.assertEqual(expected_codes, set(TAXONOMY.DELIVERY_FAILURE_CODES))
        self.assertEqual(expected_codes, set(TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS))
        for code in expected_codes:
            with self.subTest(code=code):
                self.assertTrue(TAXONOMY.is_delivery_failure_code(code))
                self.assertNotIn("legal", code)
                self.assertTrue(TAXONOMY.DELIVERY_FAILURE_REMEDIATIONS[code].strip())

        for doc in (SOURCE_DELIVERY_DOC, SOURCE_DISCOVERY_DOC, SOURCE_MANIFEST_DOC, NORMALIZED_SOURCE_DOC):
            text = doc.read_text(encoding="utf-8")
            for code in expected_codes:
                with self.subTest(doc=doc.name, code=code):
                    self.assertIn(f"`{code}`", text)

        self.assertTrue(SOURCE_DELIVERY_FIXTURES.is_dir(), f"{SOURCE_DELIVERY_FIXTURES} is missing")
        fixture_codes = set()
        acceptance_examples = {}
        for path in sorted(SOURCE_DELIVERY_FIXTURES.glob("*.provenance.yml")):
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIsInstance(document, dict, path.name)
            code = document.get("delivery_failure_code")
            self.assertIn(code, expected_codes, path.name)
            fixture_codes.add(code)
            for field in (
                "origin_url",
                "retrieved_at",
                "retrieved_by",
                "source_status",
                "delivery_failure_code",
                "delivery_failure_detail",
                "delivery_failure_remediation",
            ):
                with self.subTest(fixture=path.name, field=field):
                    self.assertIn(field, document)
            if "nvidia" in path.name or "government" in path.name:
                acceptance_examples[path.name] = document

        self.assertEqual(expected_codes, fixture_codes)
        self.assertIn("nvidia-product-javascript-required.html.provenance.yml", acceptance_examples)
        self.assertIn("government-official-error-page.html.provenance.yml", acceptance_examples)
        self.assertEqual(
            set(acceptance_examples["nvidia-product-javascript-required.html.provenance.yml"]),
            set(acceptance_examples["government-official-error-page.html.provenance.yml"]),
        )

    def test_sidecar_files_are_excluded_from_manifest_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(workspace, "raw/pdf/2601.00001v1.pdf", {"origin_url": "https://example.org/x"})

            records, _ = self.build_records(workspace)

            for record in records:
                for raw_path in record.get("raw_paths", []):
                    self.assertNotIn(".provenance.yml", raw_path, f"sidecar leaked into record {record['id']}")

    def test_checksum_mismatch_marks_record_for_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            pdf_rel = "raw/pdf/2601.00001v1.pdf"
            self.write_sidecar(
                workspace,
                pdf_rel,
                {"retrieved_by": "fetch-agent/arxiv", "checksum": "sha256:" + "0" * 64},
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            self.assertFalse(paper["provenance"]["checksum_verified"])
            self.assertTrue(paper["metadata"]["review_required"])
            self.assertTrue(any("checksum mismatch" in warning for warning in warnings))

    def test_directory_sidecar_matches_bundle_but_cannot_verify_checksum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/other/arXiv-2601.00001v1",
                {"origin_url": "https://arxiv.org/abs/2601.00001v1", "checksum": "sha256:" + "1" * 64},
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            self.assertEqual("https://arxiv.org/abs/2601.00001v1", paper["provenance"]["origin_url"])
            self.assertFalse(paper["provenance"]["checksum_verified"])
            self.assertTrue(any("directory target" in warning for warning in warnings))

    def test_null_license_is_preserved_as_known_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/other/arXiv-2601.00001v1",
                {
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "license": None,
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            self.assertIn("license", paper["provenance"])
            self.assertIsNone(paper["provenance"]["license"])
            self.assertFalse([warning for warning in warnings if "provenance license" in warning])

    def test_malformed_sidecar_degrades_to_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            sidecar = workspace / "raw" / "pdf" / "2601.00001v1.pdf.provenance.yml"
            sidecar.write_text("{[ this is not yaml\n")

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            self.assertNotIn("provenance", paper)
            self.assertTrue(any("malformed provenance sidecar" in warning for warning in warnings))

    def test_invalid_fields_are_dropped_with_warnings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://example.org/paper",
                    "retrieved_at": "not-a-timestamp",
                    "checksum": "md5:abc",
                    "surprise": "field",
                },
            )

            records, warnings = self.build_records(workspace)
            paper = self.record_by_id(records, "paper:2601.00001v1")

            provenance = paper["provenance"]
            self.assertEqual("https://example.org/paper", provenance["origin_url"])
            self.assertNotIn("retrieved_at", provenance)
            self.assertNotIn("checksum", provenance)
            self.assertTrue(any("retrieved_at must be an ISO 8601 timestamp" in warning for warning in warnings))
            self.assertTrue(any("checksum must match" in warning for warning in warnings))
            self.assertTrue(any("unknown provenance field ignored: surprise" in warning for warning in warnings))

    def test_orphan_sidecar_warns_without_failing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(workspace, "raw/pdf/missing-file.pdf", {"origin_url": "https://example.org/x"})

            _, warnings = self.build_records(workspace)

            self.assertTrue(any("target does not exist" in warning for warning in warnings))

    def test_provenance_propagates_into_normalized_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            self.write_sidecar(
                workspace,
                "raw/pdf/2601.00001v1.pdf",
                {
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "license": "CC-BY-4.0",
                    "retrieved_by": "fetch-agent/arxiv",
                },
            )
            code, _ = self.run_inventory_main(workspace)
            self.assertEqual(0, code)

            stderr = io.StringIO()
            with patched_argv("--project-root", str(workspace), "--all"):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    code = NORMALIZE.main()
            self.assertEqual(0, code)

            normalized = workspace / "sources" / "normalized" / "paper--2601.00001v1.md"
            self.assertTrue(normalized.is_file())
            frontmatter = load_frontmatter(normalized)
            provenance = frontmatter["provenance"]
            self.assertEqual("https://arxiv.org/abs/2601.00001v1", provenance["origin_url"])
            self.assertEqual("CC-BY-4.0", provenance["license"])
            self.assertEqual("fetch-agent/arxiv", provenance["retrieved_by"])

    def test_sidecar_change_updates_raw_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            records, _ = self.build_records(workspace)
            before = self.record_by_id(records, "paper:2601.00001v1")["raw_fingerprint"]

            self.write_sidecar(workspace, "raw/pdf/2601.00001v1.pdf", {"license": "CC-BY-4.0"})
            records, _ = self.build_records(workspace)
            after = self.record_by_id(records, "paper:2601.00001v1")["raw_fingerprint"]

            self.assertNotEqual(before, after, "sidecar bytes must count toward the raw fingerprint")


class BundleFileCountTests(unittest.TestCase):
    """What a bundle record's ``metadata.file_count`` counts, measured against what it admits.

    An arXiv or LaTeX bundle record declares exactly one ``raw_paths`` entry -- the bundle
    directory -- and no member list anywhere. The controller expands that directory-shaped
    entry into every regular file beneath it when it decides which delivered raw files the
    record accounts for, and the raw tree snapshot fingerprints one entry per regular file
    the same way, so the subtree is the record's unit of admission. ``file_count`` is the
    record's own account of that subtree, and the two have to count the same set.

    They did not. The count filtered members through ``should_skip``, which withholds every
    dot-prefixed path, so a file planted under a dot path inside a delivered bundle was
    admitted under the record and invisible in it. The predicate was applied to the path
    relative to the *workspace*, so a single dot component anywhere in the prefix suppressed
    the whole subtree beneath it.

    ``raw_fingerprint`` is the other half of that invisibility and is not closed here.
    `raw_fingerprint_paths` filters the same way on purpose -- it names the bytes
    normalization re-reads, not the bytes the record admits -- so a dot-prefixed member
    still reproduces a byte-identical fingerprint and triggers no re-normalization. That
    residue is asserted below rather than left to be discovered, so the count fix is not
    read as more than it is.

    The two classification tests measure against the snapshot's own rule rather than
    against a restatement of it, because a restatement is exactly what drifted the first
    time.
    """

    BUNDLE_RELATIVE = "raw/other/arXiv-2601.00001v1"
    SOURCE_ID = "paper:2601.00001v1"

    def copy_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        shutil.copytree(ARXIV_FIXTURE, workspace)
        return workspace

    def bundle_record(self, workspace: Path) -> dict:
        config = INVENTORY.load_config(workspace)
        records, _warnings, _summary = INVENTORY.build_records(workspace, config, previous_detected_at={})
        matches = [record for record in records if record.get("id") == self.SOURCE_ID]
        self.assertEqual(1, len(matches), records)
        record = matches[0]
        # The fixture pairs a PDF into this record, so ``raw_paths`` holds two entries and
        # the bundle directory is the one ``file_count`` is a count of.
        self.assertEqual(self.BUNDLE_RELATIVE, record["latex_root"], record)
        self.assertIn(self.BUNDLE_RELATIVE, record["raw_paths"], record)
        return record

    def test_a_dot_prefixed_member_moves_the_bundle_file_count(self):
        """A file the record admits must be a file the record counts, dot path or not.

        Two members are planted: one under a dot-prefixed *directory* and one dot-prefixed
        *file* at the bundle root. Both are admitted under this record by the controller's
        attribution, and neither moved ``file_count`` while the count filtered through
        ``should_skip`` -- the directory case because one dot component suppressed its whole
        subtree.

        The count is asserted as an exact total against every regular file on disk beneath
        the bundle rather than as an increment, so the assertion states what the field means
        rather than only that it changed.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            bundle = workspace / self.BUNDLE_RELATIVE

            before = self.bundle_record(workspace)
            self.assertEqual(3, before["metadata"]["file_count"], before)

            (bundle / ".build").mkdir()
            (bundle / ".build" / "main.aux").write_text("\\relax\n", encoding="utf-8")
            (bundle / ".latexmkrc").write_text("$pdf_mode = 1;\n", encoding="utf-8")

            after = self.bundle_record(workspace)

            on_disk = sorted(path.relative_to(workspace).as_posix() for path in bundle.rglob("*") if path.is_file())
            self.assertEqual(
                [
                    f"{self.BUNDLE_RELATIVE}/.build/main.aux",
                    f"{self.BUNDLE_RELATIVE}/.latexmkrc",
                    f"{self.BUNDLE_RELATIVE}/00README.json",
                    f"{self.BUNDLE_RELATIVE}/main.tex",
                    f"{self.BUNDLE_RELATIVE}/sections/intro.tex",
                ],
                on_disk,
            )
            self.assertEqual(
                len(on_disk),
                after["metadata"]["file_count"],
                "every regular file the bundle admits must be counted by the record that "
                f"admits it, dot-prefixed members included; record was {after}",
            )

    def test_a_dot_prefixed_member_still_leaves_the_raw_fingerprint_identical(self):
        """The disclosed residue, pinned: the count moves and the fingerprint does not.

        `raw_fingerprint_paths` selects the bytes normalization re-reads, and it filters
        dot-prefixed paths on purpose, so a dot-prefixed member beneath a bundle still
        triggers no re-normalization. Widening the count does not widen that, and this test
        exists so the boundary is stated by the suite rather than inferred from it. Closing
        it needs an inventory-level member list, not a wider predicate.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            bundle = workspace / self.BUNDLE_RELATIVE

            before = self.bundle_record(workspace)
            (bundle / ".latexmkrc").write_text("$pdf_mode = 1;\n", encoding="utf-8")
            after = self.bundle_record(workspace)

            self.assertEqual(
                before["metadata"]["file_count"] + 1,
                after["metadata"]["file_count"],
                after,
            )
            self.assertEqual(
                before["raw_fingerprint"],
                after["raw_fingerprint"],
                "a dot-prefixed bundle member is outside the fingerprint on purpose: this "
                f"residue is disclosed, not closed; record was {after}",
            )

    def test_a_symlinked_member_does_not_move_the_bundle_file_count(self):
        """`is_file()` resolves symlinks; the snapshot refuses them. The count follows the snapshot.

        Widening the subset was half the job. The raw tree snapshot refuses a symlink
        outright as "contains a symbolic link or junction" rather than enumerating it, so a
        symlinked member is not evidence this record admits and counting it would restore
        the same mismatch with the excluded set merely moved to the other side.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            bundle = workspace / self.BUNDLE_RELATIVE

            before = self.bundle_record(workspace)
            (bundle / "linked.tex").symlink_to(bundle / "main.tex")
            after = self.bundle_record(workspace)

            self.assertEqual(
                before["metadata"]["file_count"],
                after["metadata"]["file_count"],
                "a symlink is not a file the snapshot will enumerate, so it is not evidence "
                f"this record admits and must not move its count; record was {after}",
            )

    def test_a_multiply_linked_member_leaves_the_count_with_both_of_its_names(self):
        """A hardlink drops *both* copies from the count, because the snapshot refuses the tree.

        The snapshot admits only a "singly linked regular file", so a hardlink disqualifies
        the original as much as the new name. The count therefore falls by one rather than
        rising by one, which looks wrong until the other half is seen: for that same tree the
        snapshot refuses rather than returning entries at all.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.copy_workspace(Path(tmpdir))
            bundle = workspace / self.BUNDLE_RELATIVE

            before = self.bundle_record(workspace)
            try:
                os.link(bundle / "main.tex", bundle / "hardlinked.tex")
            except OSError as exc:  # pragma: no cover - platform without hardlink support
                self.skipTest(f"hardlinks are unavailable on this platform: {exc}")
            after = self.bundle_record(workspace)

            self.assertEqual(
                before["metadata"]["file_count"] - 1,
                after["metadata"]["file_count"],
                "both names of a hardlinked member must leave the count: the snapshot admits "
                f"only a singly linked regular file; record was {after}",
            )


if __name__ == "__main__":
    unittest.main()
