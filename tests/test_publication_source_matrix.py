from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import re
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
MATRIX_PATH = REPO_ROOT / "tests" / "fixtures" / "publication-source-matrix" / "matrix.yml"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INVENTORY = load_script_module("publication_source_matrix_inventory", "source_inventory.py")
NORMALIZE = load_script_module("publication_source_matrix_normalize", "normalize_sources.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and ".locks" not in path.relative_to(root).parts
    }


def raw_snapshot(workspace: Path) -> dict[str, str]:
    return tree_snapshot(workspace / "raw")


def canonical_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def capture_main(function, arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = function(arguments)
    return int(code or 0), stdout.getvalue(), stderr.getvalue()


@contextlib.contextmanager
def inert_external_boundaries():
    refused = AssertionError("publication source matrix attempted an external operation")
    with (
        mock.patch.object(socket, "create_connection", side_effect=refused),
        mock.patch.object(urllib.request, "urlopen", side_effect=refused),
        mock.patch.object(NORMALIZE.subprocess, "run", side_effect=refused),
    ):
        yield


class PublicationSourceMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))

    def write_fixture_file(self, path: Path, item: dict, sentinel: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoding = item.get("encoding", "utf8")
        replace = lambda value: str(value).replace("__SENTINEL__", str(sentinel))
        if encoding == "hex":
            path.write_bytes(bytes.fromhex(item["hex_bytes"]))
        elif encoding == "invalid_utf8":
            path.write_bytes(
                replace(item["prefix"]).encode()
                + bytes.fromhex(item["invalid_hex"])
                + replace(item["suffix"]).encode()
            )
        elif encoding == "repeated_text":
            path.write_text(
                replace(item["prefix"])
                + replace(item["repeat_text"]) * int(item["repeat_count"])
                + replace(item["suffix"]),
                encoding="utf-8",
            )
        else:
            path.write_text(replace(item["content"]), encoding="utf-8")

    def build_workspace(self, root: Path) -> tuple[Path, Path]:
        workspace = root / "workspace"
        sentinel = root / "source-content-must-not-execute"
        (workspace / "sources" / "normalized").mkdir(parents=True)
        config = {
            "project": {"name": "publication-source-matrix"},
            "raw": {"immutable": True, "source_roots": self.matrix["source_roots"]},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
                "default_status": "discovered",
            },
            "integrations": {
                "codebase_analysis": {
                    "enabled": True,
                    "read_only": True,
                    "output_dir": "sources/code_wikis",
                }
            },
        }
        (workspace / "research.yml").write_text(
            yaml.safe_dump(config, sort_keys=False),
            encoding="utf-8",
        )
        (workspace / "log.md").write_text("# Publication Source Matrix Log\n", encoding="utf-8")

        for payload in self.matrix["payloads"]:
            self.write_fixture_file(workspace / payload["relative_path"], payload, sentinel)
        for sidecar in self.matrix["malformed_sidecars"]:
            path = workspace / f"{sidecar['target']}.provenance.yml"
            path.write_text(sidecar["content"], encoding="utf-8")
        for bundle in self.matrix["latex_bundles"]:
            for item in bundle["files"]:
                self.write_fixture_file(workspace / bundle["root"] / item["relative_path"], item, sentinel)
        for item in self.matrix["code_repository"]["files"]:
            self.write_fixture_file(
                workspace / self.matrix["code_repository"]["root"] / item["relative_path"],
                item,
                sentinel,
            )
        for case in self.matrix["identity_cases"]:
            pdf = workspace / case["raw_path"]
            pdf.parent.mkdir(parents=True, exist_ok=True)
            pdf.write_bytes(b"%PDF-1.4\n% SYNTHETIC provider/parser identity fixture\n%%EOF\n")
            sidecar = pdf.with_name(f"{pdf.name}.provenance.yml")
            sidecar.write_text(
                yaml.safe_dump(
                    {
                        "origin_url": f"https://example.org/synthetic/{case['method'].lower()}",
                        "retrieved_by": "fetch_sources.py/arxiv",
                        "academic_provider": "synthetic_replay",
                        "academic_source_type": "preprint",
                        "title": case["provider_title"],
                        "authors": case["provider_authors"],
                        "license": "MIT",
                        "notes": "Synthetic replay identity; no provider request was made.",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
        return workspace, sentinel

    def run_inventory(self, workspace: Path, *arguments: str) -> tuple[int, str, str]:
        return capture_main(INVENTORY.main, ["--project-root", str(workspace), *arguments])

    def run_normalize(self, workspace: Path, *arguments: str) -> tuple[int, str, str]:
        return capture_main(NORMALIZE.main, ["--project-root", str(workspace), *arguments])

    def manifest(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "manifest.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def record_for_raw_path(self, records: list[dict], raw_path: str) -> dict:
        matches = [record for record in records if raw_path in record.get("raw_paths", [])]
        self.assertEqual(1, len(matches), f"expected one record for {raw_path}: {matches}")
        return matches[0]

    def output_for_record(self, workspace: Path, record: dict) -> Path:
        return NORMALIZE.normalized_output_path_for_record(record, workspace / "sources" / "normalized")

    def pdf_extractor(self, *, recovered_scanned: bool = False):
        identities = {case["raw_path"]: case for case in self.matrix["identity_cases"]}

        def extract(_pdftotext_path: str, _pdf_path: Path, pdf_label: str):
            if pdf_label == "raw/papers/scanned.pdf" and not recovered_scanned:
                return "", [f"{pdf_label}: synthetic empty extraction"], True
            case = identities.get(pdf_label)
            title = case["parser_title"] if case else "Ambiguous Synthetic PDF Fixture"
            marker = f"{case['method'].upper()}_PDF_BODY_MARKER" if case else "AMBIGUOUS_PDF_BODY_MARKER"
            body = " ".join(
                [
                    f"{marker} is deterministic parser evidence with no provider contact.",
                    "The repeated body keeps this one-page synthetic extraction above the OCR threshold.",
                ]
                * 4
            )
            return f"{title}\nAbstract\n{body}\n1 Introduction\n{body}\n", [], True

        return extract

    @contextlib.contextmanager
    def offline_normalization(self, *, recovered_scanned: bool = False):
        with (
            inert_external_boundaries(),
            mock.patch.object(NORMALIZE.shutil, "which", return_value="pdftotext"),
            mock.patch.object(
                NORMALIZE,
                "extract_pdf_text",
                side_effect=self.pdf_extractor(recovered_scanned=recovered_scanned),
            ),
        ):
            yield

    def assert_raw_unchanged(self, workspace: Path, before: dict[str, str]) -> None:
        self.assertEqual(before, raw_snapshot(workspace))

    def normalizer_audit(self, workspace: Path) -> dict[str, tuple[str | None, int | None]]:
        audit: dict[str, tuple[str | None, int | None]] = {}
        for path in sorted((workspace / "sources" / "normalized").glob("*.md")):
            frontmatter = NORMALIZE.read_output_frontmatter(path)
            identity = frontmatter.get("normalizer") if isinstance(frontmatter.get("normalizer"), dict) else {}
            audit[str(frontmatter.get("source_id"))] = (identity.get("name"), identity.get("version"))
        return audit

    def orphaned_normalized_source_ids(self, workspace: Path, records: list[dict]) -> set[str]:
        manifest_ids = {record["id"] for record in records}
        normalized_ids = {
            str(NORMALIZE.read_output_frontmatter(path).get("source_id"))
            for path in (workspace / "sources" / "normalized").glob("*.md")
        }
        return normalized_ids - manifest_ids

    def test_matrix_contract_is_complete(self):
        contract = self.matrix["coverage_contract"]
        self.assertEqual(
            {"markdown", "html", "pdf", "latex", "code", "json", "csv", "url", "binary"},
            set(contract["required_formats"]),
        )
        self.assertEqual(
            {
                "inventory_dry_run",
                "inventory_write",
                "normalize_dry_run",
                "normalize_all",
                "normalize_selected",
                "normalize_incremental",
                "normalize_force",
            },
            set(contract["required_modes"]),
        )
        self.assertEqual({"modify", "rename", "delete", "normalizer_version"}, set(contract["required_drift"]))
        self.assertEqual(
            {
                "malformed_sidecar",
                "duplicate_names",
                "invalid_encoding",
                "nested_frontmatter",
                "formulas",
                "huge_line",
                "active_content",
                "ambiguous_pdf_pairing",
                "extraction_loss",
                "interruption_retry",
                "network_inert",
                "raw_immutable",
            },
            set(contract["required_hazards"]),
        )
        self.assertEqual({"GPTQ", "AWQ", "KIVI", "TurboQuant"}, {case["method"] for case in self.matrix["identity_cases"]})
    def test_complete_pipeline_modes_drift_loss_and_identity_matrix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, sentinel = self.build_workspace(Path(tmpdir))
            initial_tree = tree_snapshot(workspace)
            initial_raw = raw_snapshot(workspace)

            with inert_external_boundaries():
                dry_inventory = self.run_inventory(workspace, "--dry-run", "--report", "--format", "json")
            self.assertEqual(0, dry_inventory[0], dry_inventory[2])
            self.assertEqual(initial_tree, tree_snapshot(workspace))
            self.assertFalse((workspace / "sources" / "manifest.jsonl").exists())
            inventory_report = json.loads(dry_inventory[1])
            self.assertGreaterEqual(inventory_report["pairing_counts"]["ambiguous"], 3)
            self.assertTrue(any("malformed provenance sidecar" in warning for warning in inventory_report["warnings"]))

            with inert_external_boundaries():
                write_inventory = self.run_inventory(workspace, "--report", "--format", "json")
            self.assertEqual(0, write_inventory[0], write_inventory[2])
            self.assert_raw_unchanged(workspace, initial_raw)
            records = self.manifest(workspace)
            kinds = {kind: sum(record.get("kind") == kind for record in records) for kind in {r["kind"] for r in records}}
            self.assertGreaterEqual(kinds.get("html", 0), 4)
            self.assertGreaterEqual(kinds.get("pdf", 0), 6)
            self.assertGreaterEqual(kinds.get("paper", 0), 3)
            self.assertEqual(1, kinds.get("table", 0))
            self.assertEqual(1, kinds.get("web_link", 0))
            self.assertEqual(1, kinds.get("codebase_architecture", 0))
            self.assertGreaterEqual(kinds.get("unknown", 0), 2)
            expected_kinds = {
                "raw/markdown/nested-frontmatter.md": "markdown",
                "raw/web/one/report.html": "html",
                "raw/papers/scanned.pdf": "pdf",
                "raw/data/records.json": "unknown",
                "raw/data/matrix.csv": "table",
                "raw/links/links.txt": "web_link",
                "raw/binary/payload.bin": "unknown",
            }
            for raw_path, expected_kind in expected_kinds.items():
                with self.subTest(raw_path=raw_path):
                    self.assertEqual(expected_kind, self.record_for_raw_path(records, raw_path)["kind"])
            duplicate_one = self.record_for_raw_path(records, "raw/web/one/report.html")
            duplicate_two = self.record_for_raw_path(records, "raw/web/two/report.html")
            self.assertNotEqual(duplicate_one["id"], duplicate_two["id"])
            ambiguous = [record for record in records if record.get("pairing_status") == "ambiguous"]
            self.assertGreaterEqual(len(ambiguous), 3)
            self.assertTrue(all(record.get("metadata", {}).get("review_required") for record in ambiguous))

            before_dry_normalize = tree_snapshot(workspace)
            with self.offline_normalization():
                dry_normalize = self.run_normalize(workspace, "--all", "--dry-run", "--format", "json")
            self.assertEqual(0, dry_normalize[0], dry_normalize[2])
            self.assertEqual(before_dry_normalize, tree_snapshot(workspace))
            dry_report = json.loads(dry_normalize[1])
            self.assertEqual(dry_report["summary"]["selected"], dry_report["summary"]["planned"])
            self.assertEqual(dry_report["summary"]["planned"], dry_report["summary"]["dry_run"])
            self.assertGreaterEqual(dry_report["summary"]["skipped_unsupported"], 3)

            selected_raw = "raw/web/one/report.html"
            selected_record = self.record_for_raw_path(records, selected_raw)
            before_selected = raw_snapshot(workspace)
            with self.offline_normalization():
                selected = self.run_normalize(
                    workspace,
                    "--source-id",
                    selected_record["id"],
                    "--format",
                    "json",
                )
            self.assertEqual(0, selected[0], selected[2])
            selected_report = json.loads(selected[1])
            self.assertEqual("source_id", selected_report["selector"])
            self.assertEqual(1, selected_report["summary"]["created"])
            self.assert_raw_unchanged(workspace, before_selected)

            before_incremental = raw_snapshot(workspace)
            with self.offline_normalization():
                incremental = self.run_normalize(workspace, "--format", "json")
            self.assertEqual(0, incremental[0], incremental[2])
            incremental_report = json.loads(incremental[1])
            self.assertEqual("pending", incremental_report["selector"])
            self.assertGreater(incremental_report["summary"]["created"], 5)
            self.assertEqual(1, incremental_report["summary"]["partial"])
            self.assert_raw_unchanged(workspace, before_incremental)

            with self.offline_normalization():
                all_mode = self.run_normalize(workspace, "--all", "--format", "json")
            self.assertEqual(0, all_mode[0], all_mode[2])
            all_report = json.loads(all_mode[1])
            self.assertEqual(0, all_report["summary"]["planned"])
            self.assertEqual(all_report["summary"]["selected"], all_report["summary"]["skipped_existing"])

            with self.offline_normalization():
                forced = self.run_normalize(workspace, "--all", "--force", "--format", "json")
            self.assertEqual(0, forced[0], forced[2])
            forced_report = json.loads(forced[1])
            self.assertEqual(forced_report["summary"]["selected"], forced_report["summary"]["updated"])
            self.assertFalse(sentinel.exists())

            records = self.manifest(workspace)
            unsupported = [record for record in records if record.get("kind") in {"markdown", "unknown"}]
            self.assertGreaterEqual(len(unsupported), 3)
            unsupported_paths = {raw_path for record in unsupported for raw_path in record.get("raw_paths", [])}
            self.assertTrue(
                {
                    "raw/markdown/nested-frontmatter.md",
                    "raw/data/records.json",
                    "raw/binary/payload.bin",
                }
                <= unsupported_paths
            )
            for record in unsupported:
                self.assertFalse(self.output_for_record(workspace, record).exists())
            warning_blob = "\n".join(forced_report["warnings"])
            self.assertTrue(all(record["id"] in warning_blob for record in unsupported))

            raw_html_one = (workspace / selected_raw).read_text(encoding="utf-8")
            self.assertIn("ACTIVE_SCRIPT_MARKER", raw_html_one)
            html_one = self.output_for_record(workspace, self.record_for_raw_path(records, selected_raw)).read_text(encoding="utf-8")
            self.assertIn("HTML_ONE_VISIBLE_MARKER", html_one)
            self.assertNotIn("ACTIVE_SCRIPT_MARKER", html_one)
            invalid_html = self.output_for_record(
                workspace,
                self.record_for_raw_path(records, "raw/web/invalid-encoding.html"),
            ).read_text(encoding="utf-8")
            self.assertIn("INVALID_ENCODING_MARKER", invalid_html)
            self.assertIn("�", invalid_html)
            huge_html = self.output_for_record(
                workspace,
                self.record_for_raw_path(records, "raw/web/huge-line.html"),
            ).read_text(encoding="utf-8")
            self.assertIn("HUGE_LINE_MARKER", huge_html)
            self.assertIn("HUGE_LINE_TAIL", huge_html)

            table = self.output_for_record(
                workspace,
                self.record_for_raw_path(records, "raw/data/matrix.csv"),
            ).read_text(encoding="utf-8")
            self.assertIn("GPTQ", table)
            self.assertIn("E=mc^2", table)
            formula_record = next(record for record in records if record.get("latex_root") == "raw/latex-a/formula")
            formula = self.output_for_record(workspace, formula_record).read_text(encoding="utf-8")
            self.assertIn("LATEX_INCLUDE_MARKER", formula)
            self.assertIn("E=mc^2", formula)
            self.assertFalse(sentinel.exists())

            link_record = self.record_for_raw_path(records, "raw/links/links.txt")
            link_output = self.output_for_record(workspace, link_record).read_text(encoding="utf-8")
            self.assertIn("Network content has not been fetched", link_output)
            code_record = next(record for record in records if record.get("kind") == "codebase_architecture")
            code_output = self.output_for_record(workspace, code_record).read_text(encoding="utf-8")
            self.assertIn("Adapter output has not been recorded", code_output)
            self.assertNotIn("ACTIVE_CODE_MARKER", code_output)

            comparisons: dict[str, str] = {}
            identities = {case["raw_path"]: case for case in self.matrix["identity_cases"]}
            for raw_path, case in identities.items():
                record = self.record_for_raw_path(records, raw_path)
                frontmatter = NORMALIZE.read_output_frontmatter(self.output_for_record(workspace, record))
                self.assertEqual(case["provider_title"], frontmatter["title"])
                self.assertEqual("provider", frontmatter["title_source"])
                parser_title = frontmatter["extracted_title"]
                if parser_title == case["provider_title"]:
                    comparison = "exact_match"
                elif canonical_title(parser_title) == canonical_title(case["provider_title"]):
                    comparison = "normalized_match"
                else:
                    comparison = "mismatch_visible"
                comparisons[case["method"]] = comparison
                self.assertEqual(case["expected_comparison"], comparison)
            self.assertEqual("mismatch_visible", comparisons["KIVI"])

            scanned = self.record_for_raw_path(records, "raw/papers/scanned.pdf")
            scanned_frontmatter = NORMALIZE.read_output_frontmatter(self.output_for_record(workspace, scanned))
            self.assertEqual("partial", scanned_frontmatter["status"])
            self.assertTrue(scanned_frontmatter["needs_ocr"])
            with self.offline_normalization(recovered_scanned=True):
                recovered = self.run_normalize(
                    workspace,
                    "--source-id",
                    scanned["id"],
                    "--force",
                    "--format",
                    "json",
                )
            self.assertEqual(0, recovered[0], recovered[2])
            recovered_report = json.loads(recovered[1])
            self.assertEqual("content_extracted", recovered_report["actions"][0]["status"])
            recovered_text = self.output_for_record(workspace, scanned).read_text(encoding="utf-8")
            self.assertIn("AMBIGUOUS_PDF_BODY_MARKER", recovered_text)

            audit = self.normalizer_audit(workspace)
            self.assertTrue(audit)
            self.assertEqual({(NORMALIZE.NORMALIZER_NAME, NORMALIZE.NORMALIZER_VERSION)}, set(audit.values()))
            version_target = self.output_for_record(workspace, selected_record)
            version_target.write_text(
                version_target.read_text(encoding="utf-8").replace(
                    f"version: {NORMALIZE.NORMALIZER_VERSION}",
                    "version: 0",
                    1,
                ),
                encoding="utf-8",
            )
            self.assertEqual((NORMALIZE.NORMALIZER_NAME, 0), self.normalizer_audit(workspace)[selected_record["id"]])
            with self.offline_normalization():
                version_repair = self.run_normalize(
                    workspace,
                    "--source-id",
                    selected_record["id"],
                    "--force",
                    "--format",
                    "json",
                )
            self.assertEqual(0, version_repair[0], version_repair[2])
            self.assertEqual(
                (NORMALIZE.NORMALIZER_NAME, NORMALIZE.NORMALIZER_VERSION),
                self.normalizer_audit(workspace)[selected_record["id"]],
            )

            html_path = workspace / selected_raw
            html_path.write_text(
                html_path.read_text(encoding="utf-8") + "<p>MODIFIED_HTML_MARKER</p>\n",
                encoding="utf-8",
            )
            before_modify_inventory = raw_snapshot(workspace)
            with inert_external_boundaries():
                modify_inventory = self.run_inventory(workspace)
            self.assertEqual(0, modify_inventory[0], modify_inventory[2])
            self.assert_raw_unchanged(workspace, before_modify_inventory)
            with self.offline_normalization():
                modified = self.run_normalize(workspace, "--format", "json")
            self.assertEqual(0, modified[0], modified[2])
            modified_report = json.loads(modified[1])
            self.assertEqual(1, modified_report["summary"]["stale"])
            self.assertEqual(1, modified_report["summary"]["updated"])
            self.assertIn("MODIFIED_HTML_MARKER", version_target.read_text(encoding="utf-8"))

            records_before_rename = self.manifest(workspace)
            old_csv = self.record_for_raw_path(records_before_rename, "raw/data/matrix.csv")
            old_csv_output = self.output_for_record(workspace, old_csv)
            (workspace / "raw" / "data" / "matrix.csv").rename(workspace / "raw" / "data" / "matrix-renamed.csv")
            before_rename_inventory = raw_snapshot(workspace)
            with inert_external_boundaries():
                rename_inventory = self.run_inventory(workspace)
            self.assertEqual(0, rename_inventory[0], rename_inventory[2])
            self.assert_raw_unchanged(workspace, before_rename_inventory)
            renamed_records = self.manifest(workspace)
            self.assertNotIn(old_csv["id"], {record["id"] for record in renamed_records})
            new_csv = self.record_for_raw_path(renamed_records, "raw/data/matrix-renamed.csv")
            self.assertNotEqual(old_csv["id"], new_csv["id"])
            self.assertTrue(old_csv_output.exists())
            with self.offline_normalization():
                rename_plan = self.run_normalize(workspace, "--dry-run", "--format", "json")
            self.assertEqual(0, rename_plan[0], rename_plan[2])
            rename_report = json.loads(rename_plan[1])
            self.assertIn(new_csv["id"], {action["source_id"] for action in rename_report["actions"]})

            deleted_record = self.record_for_raw_path(renamed_records, "raw/web/two/report.html")
            deleted_output = self.output_for_record(workspace, deleted_record)
            (workspace / "raw" / "web" / "two" / "report.html").unlink()
            before_delete_inventory = raw_snapshot(workspace)
            with inert_external_boundaries():
                delete_inventory = self.run_inventory(workspace)
            self.assertEqual(0, delete_inventory[0], delete_inventory[2])
            self.assert_raw_unchanged(workspace, before_delete_inventory)
            after_delete = self.manifest(workspace)
            self.assertNotIn(deleted_record["id"], {record["id"] for record in after_delete})
            self.assertTrue(deleted_output.exists())
            orphans = self.orphaned_normalized_source_ids(workspace, after_delete)
            self.assertTrue({old_csv["id"], deleted_record["id"]} <= orphans)
            self.assertFalse(sentinel.exists())

    def test_interruption_leaves_atomic_temp_state_and_retry_completes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace, sentinel = self.build_workspace(Path(tmpdir))
            with inert_external_boundaries():
                inventory = self.run_inventory(workspace)
            self.assertEqual(0, inventory[0], inventory[2])
            before = raw_snapshot(workspace)
            normalized_root = workspace / "sources" / "normalized"
            original_replace = Path.replace
            replacements = 0

            def interrupt_second_replace(path: Path, target: Path):
                nonlocal replacements
                if path.parent == normalized_root and path.name.startswith(".") and path.name.endswith(".tmp"):
                    replacements += 1
                    if replacements == 2:
                        raise KeyboardInterrupt("synthetic normalization interruption")
                return original_replace(path, target)

            args = NORMALIZE.parse_args(["--project-root", str(workspace), "--all", "--format", "json"])
            with (
                self.offline_normalization(),
                mock.patch.object(Path, "replace", new=interrupt_second_replace),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(KeyboardInterrupt),
            ):
                NORMALIZE.run_normalization(args)

            finals_after_interrupt = sorted(normalized_root.glob("*.md"))
            temps_after_interrupt = sorted(normalized_root.glob(".*.tmp"))
            self.assertEqual(1, len(finals_after_interrupt))
            self.assertEqual(1, len(temps_after_interrupt))
            self.assert_raw_unchanged(workspace, before)

            with self.offline_normalization():
                retry = self.run_normalize(workspace, "--format", "json")
            self.assertEqual(0, retry[0], retry[2])
            retry_report = json.loads(retry[1])
            self.assertGreater(retry_report["summary"]["created"], 1)
            self.assertEqual([], sorted(normalized_root.glob(".*.tmp")))
            records = self.manifest(workspace)
            eligible = NORMALIZE.eligible_records(workspace, records)
            self.assertTrue(
                all(self.output_for_record(workspace, item.record).is_file() for item in eligible),
                "retry must complete every remaining eligible source",
            )
            self.assert_raw_unchanged(workspace, before)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
