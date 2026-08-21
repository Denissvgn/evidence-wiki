"""CR-2's backward-compatibility promise: no adapters configured, no change in behaviour.

CR-2 added a config section, a kind, a contract validator, a version stamp, and a
subprocess execution path. Every one of those is reachable from code that also runs for
a workspace that configures none of it, so "additive" has to be checked rather than
asserted. These tests check it on `arxiv-source-project`, an existing fixture whose
three records are ordinary native output, and they compare whole artifacts rather than
spot fields — a regression that only shows up in one frontmatter key is exactly the kind
this is here to catch.

The comparisons are differential where they can be: the same fixture run twice with one
variable changed. That is stronger than a golden file, which drifts into being updated
rather than believed.
"""

import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from tests._script_loader import load_script as load_script_module

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "arxiv-source-project"

# Mapped to a kind this fixture contains none of, and pointing at a path that cannot
# exist: if the section were ever consulted for these records, normalization would
# report a failed action instead of quietly agreeing with the baseline.
INERT_ADAPTER = {
    "kinds": ["structured_data"],
    "provider": "command",
    "command": ["/nonexistent/adapter-must-not-run"],
    "name": "must-not-run",
    "version": "9.9.9",
}

# Written by the run, not derived from workspace content: comparing them would only
# assert that two runs happened in the same second, which is a flake rather than a
# check. `timestamp` is normalization's, `generated_at` is the verifier's — both at
# second resolution.
VOLATILE_REPORT_KEYS = frozenset({"timestamp", "generated_at"})

# The equivalent inside a record: the moment it was written, also at second resolution.
NORMALIZED_AT_RE = re.compile(r"^normalized_at: .*$", re.MULTILINE)

# And inside a manifest entry: when inventory first saw the file.
MANIFEST_VOLATILE_KEYS = frozenset({"detected_at"})


INVENTORY = load_script_module("compat_inventory", "source_inventory.py")
NORMALIZE = load_script_module("compat_normalize", "normalize_sources.py")
VERIFY = load_script_module("compat_normalize_verify", "normalize_verify.py")
LINT = load_script_module("compat_lint", "lint.py")
CONTRACT = load_script_module("compat_contract", "_normalized_contract.py")


class NoAdapterBackwardCompatTests(unittest.TestCase):
    # -- harness -----------------------------------------------------------------

    def build(self, root: Path, *, name: str, adapter: dict | None = None) -> Path:
        workspace = root / name
        shutil.copytree(FIXTURE, workspace)
        if adapter is not None:
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["normalization"] = {"adapters": [adapter]}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return workspace

    def run_inventory(self, workspace: Path) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = INVENTORY.main(["--project-root", str(workspace), "--format", "json"])
        self.assertEqual(0, code)

    def run_normalize(self, workspace: Path, *extra: str) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(
                ["--project-root", str(workspace), "--all", "--format", "json", *extra]
            )
        self.assertEqual(0, code, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def run_verify(self, workspace: Path) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.main(["--project-root", str(workspace)])
        return int(code or 0), json.loads(stdout.getvalue() or "{}")

    def run_lint(self, workspace: Path) -> dict:
        return LINT.run_checks(workspace, LINT.load_config(workspace))

    # -- comparable snapshots ----------------------------------------------------

    def records(self, workspace: Path) -> dict[str, bytes]:
        root = workspace / "sources" / "normalized"
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*.md"))
        }

    def comparable_records(self, workspace: Path) -> dict[str, str]:
        """Records with the one wall-clock field masked.

        `normalized_at` says when the record was written, at second resolution. Two
        pipelines that straddle a second boundary differ there and nowhere else, which
        is a flake rather than a finding — so it is masked for cross-workspace
        comparison and left intact where a rewrite is what's being detected.
        """
        masked: dict[str, str] = {}
        for name, content in self.records(workspace).items():
            text = content.decode("utf-8")
            replaced, count = NORMALIZED_AT_RE.subn("normalized_at: <masked>", text)
            # If the field is ever renamed, masking becomes a silent no-op and the
            # comparison quietly starts asserting that two runs shared a second.
            self.assertEqual(1, count, f"{name}: expected exactly one normalized_at line")
            masked[name] = replaced
        return masked

    def stable_report(self, report: dict) -> dict:
        # A report that carries no volatile key means the key was renamed and this
        # comparison quietly started comparing wall-clock values. Fail here instead.
        self.assertTrue(
            VOLATILE_REPORT_KEYS & set(report),
            f"no volatile key to drop; update VOLATILE_REPORT_KEYS for {report.get('document_type')}",
        )
        return {key: value for key, value in report.items() if key not in VOLATILE_REPORT_KEYS}

    def comparable_manifest(self, workspace: Path) -> list[dict]:
        """Manifest entries with first-seen timestamps masked, for the same reason."""
        entries = [
            json.loads(line)
            for line in (workspace / "sources" / "manifest.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        for entry in entries:
            self.assertTrue(
                MANIFEST_VOLATILE_KEYS & set(entry),
                f"{entry.get('id')}: no volatile key to mask; update MANIFEST_VOLATILE_KEYS",
            )
            for key in MANIFEST_VOLATILE_KEYS & set(entry):
                entry[key] = "<masked>"
        return entries

    def stable_lint(self, results: dict) -> dict:
        return {"issues": results["issues"], "stats": results["stats"]}

    def pipeline(self, workspace: Path) -> dict:
        """Everything a no-config workspace produces, in one comparable bundle."""
        self.run_inventory(workspace)
        report = self.run_normalize(workspace)
        verify_code, verify_report = self.run_verify(workspace)
        return {
            "manifest": self.comparable_manifest(workspace),
            "records": self.comparable_records(workspace),
            "report": self.stable_report(report),
            "lint": self.stable_lint(self.run_lint(workspace)),
            "verify": (verify_code, self.stable_report(verify_report)),
        }

    # -- the promise -------------------------------------------------------------

    def test_declaring_an_adapter_for_an_absent_kind_changes_nothing(self):
        """The one variable is whether `normalization:` exists at all.

        A workspace that never opts in must be unaffected by the section's existence,
        and a workspace that opts in for a kind it does not have must be equally
        unaffected. Comparing whole artifacts — manifest, every record byte for byte,
        both reports, the lint payload — is what makes "unaffected" mean something.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            without = self.pipeline(self.build(Path(tmpdir), name="no-section"))
            with_section = self.pipeline(
                self.build(Path(tmpdir), name="inert-section", adapter=INERT_ADAPTER)
            )

        self.assertEqual(without["manifest"], with_section["manifest"])
        self.assertEqual(without["records"], with_section["records"])
        self.assertEqual(without["report"], with_section["report"])
        self.assertEqual(without["lint"], with_section["lint"])
        self.assertEqual(without["verify"], with_section["verify"])
        # And the baseline is not vacuous: there is real native output to compare.
        self.assertEqual(3, len(without["records"]))
        self.assertEqual(0, without["report"]["summary"]["methods"]["adapter"])

    def test_a_configured_command_is_never_executed_for_a_kind_it_does_not_map(self):
        """Configuring an adapter authorizes execution for its kinds and nothing else.

        The command path cannot exist, so any attempt to run it would surface as a
        failed action and a non-zero exit rather than as a silent no-op.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="inert", adapter=INERT_ADAPTER)
            self.run_inventory(workspace)
            report = self.run_normalize(workspace)

        self.assertEqual(0, report["summary"]["failed"])
        self.assertEqual(3, report["summary"]["created"])
        self.assertEqual([], [action for action in report["actions"] if action.get("adapter")])

    def test_a_second_run_rewrites_nothing(self):
        """The version stamp must not perma-stale the records it was added to.

        `is_stale` keys on `NORMALIZER_VERSION`, which A1 bumped. If anything in that
        comparison drifted, every run would rewrite every record — the failure B4 found
        on the adapter side, checked here for the native side.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="idempotent")
            self.run_inventory(workspace)
            self.run_normalize(workspace)
            first = self.records(workspace)

            second_report = self.run_normalize(workspace)
            second = self.records(workspace)

        self.assertEqual(first, second)
        self.assertEqual(3, second_report["summary"]["skipped_existing"])
        self.assertEqual(0, second_report["summary"]["created"])
        self.assertEqual(0, second_report["summary"]["updated"])

    # -- records written before the contract existed -----------------------------

    def strip_version_stamp(self, workspace: Path) -> Path:
        """Turn a record back into what this package wrote before A1."""
        record = workspace / "sources" / "normalized" / "paper--2601.00001v1.md"
        record.write_text(
            record.read_text(encoding="utf-8").replace("normalized_format: 1\n", ""),
            encoding="utf-8",
        )
        return record

    def test_a_record_written_before_the_contract_is_still_accepted(self):
        """Absent `normalized_format` reads as a legacy native record, not a violation.

        Existing workspaces are full of these. Lint may only widen what it accepts, so
        the record must produce no contract finding and must not be counted as foreign.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="legacy")
            self.run_inventory(workspace)
            self.run_normalize(workspace)
            record = self.strip_version_stamp(workspace)

            frontmatter, _, _ = CONTRACT.split_record(record.read_text(encoding="utf-8"))
            self.assertNotIn("normalized_format", frontmatter)
            self.assertEqual(
                CONTRACT.LEGACY_NORMALIZED_FORMAT_VERSION,
                CONTRACT.effective_format_version(frontmatter),
            )

            verify_code, verify_report = self.run_verify(workspace)
            results = self.run_lint(workspace)

        self.assertEqual(VERIFY.EXIT_OK, verify_code)
        self.assertEqual("verified", verify_report["overall_result"])
        self.assertEqual(0, results["stats"]["sources_foreign_normalized"])
        self.assertEqual(0, results["stats"]["normalized_contract_violations"])
        self.assertNotIn(
            "normalized_record_contract_violation",
            {issue["category"] for issue in results["issues"]},
        )

    # The frontmatter keys a native record emits. Pinned, not derived: this is the
    # shape a host parses, so adding or removing one is a change to the public record
    # and should require saying so here. `normalized_format` and `rendered_coverage`
    # are CR-2's two additions; `structured_view` is CR-7's one, and it lands on every
    # record — `frontmatter_for` builds one flat mapping with every key present, and a
    # record with no structured view to bind carries the key as `null` rather than
    # omitting it. Everything else predates all of them.
    NATIVE_FRONTMATTER_KEYS = frozenset(
        {
            "abstract_confidence", "academic", "arxiv_id", "authors",
            "codebase_artifact_checksums", "codebase_artifact_manifest",
            "codebase_artifact_paths", "codebase_artifact_provenance",
            "codebase_execution_scope", "codebase_intake_status", "codebase_repo",
            "codebase_revision", "codebase_tool", "confidence", "content_hash",
            "created", "date", "doi", "entrypoint", "evidence_usable",
            "extracted_title", "extraction_method", "fetch_status", "language",
            "latex_root", "manifest_path", "needs_ocr", "normalized_at",
            "normalized_format", "normalizer", "openalex_id", "parse_warnings",
            "pdf_extractor", "provenance", "provider", "raw_fingerprint", "raw_paths",
            "raw_pdf", "references_source_ids", "rendered_coverage", "repo_full_name",
            "source_id", "source_kind", "standards", "status", "structured_view",
            "title", "title_confidence", "title_source", "type",
            "unusable_evidence_reasons", "updated", "url", "venue",
        }
    )

    def test_the_native_record_shape_gained_only_what_cr_2_declared(self):
        """CR-2 may add fields to the record; it may not add them quietly.

        The differential tests above compare current output to current output, so a
        field added to every record is invisible to them. This pins the shape itself,
        which is the only place a silent addition shows up.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="shape")
            self.run_inventory(workspace)
            self.run_normalize(workspace)
            shapes = {
                name: frozenset(CONTRACT.split_record(content.decode("utf-8"))[0])
                for name, content in self.records(workspace).items()
            }

        self.assertEqual(3, len(shapes))
        for name, keys in shapes.items():
            with self.subTest(record=name):
                self.assertEqual(self.NATIVE_FRONTMATTER_KEYS, keys)

    def test_a_native_record_binds_no_structured_view_and_writes_no_sidecar(self):
        """CR-7's field lands on every record and is null where there is nothing to bind.

        `frontmatter_for` builds one flat mapping with every key present, so the key is
        unconditional — that is what NATIVE_FRONTMATTER_KEYS pins. What must not happen
        is a *file* appearing beside a paper or a link record: a sidecar no record
        declares is a contract violation, and `normalize verify` would say so.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="no-sidecar")
            self.run_inventory(workspace)
            self.run_normalize(workspace)
            sidecars = sorted(
                path.name
                for path in (workspace / "sources" / "normalized").rglob("*.structured.json")
            )
            frontmatters = {
                name: CONTRACT.split_record(content.decode("utf-8"))[0]
                for name, content in self.records(workspace).items()
            }
            verify_code, verify_report = self.run_verify(workspace)

        self.assertEqual([], sidecars)
        self.assertEqual(3, len(frontmatters))
        for name, frontmatter in frontmatters.items():
            with self.subTest(record=name):
                self.assertIn("structured_view", frontmatter)
                self.assertIsNone(frontmatter["structured_view"])
        self.assertEqual(VERIFY.EXIT_OK, verify_code)
        self.assertEqual("verified", verify_report["overall_result"])

    def test_regenerating_a_legacy_record_changes_only_the_version_stamp(self):
        """The upgrade path for records an existing workspace already has.

        Re-normalizing a pre-contract record must restore the version stamp without
        touching the body or any other field — the first run after upgrading rewrites
        records, and that rewrite has to be inert apart from the stamp.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build(Path(tmpdir), name="regenerate")
            self.run_inventory(workspace)
            self.run_normalize(workspace)
            record = self.strip_version_stamp(workspace)
            before_frontmatter, before_body, _ = CONTRACT.split_record(
                record.read_text(encoding="utf-8")
            )

            # A legacy record is not stale on its own — the stored normalizer version
            # already matches — so regeneration has to be asked for.
            self.run_normalize(workspace, "--force")
            after_frontmatter, after_body, _ = CONTRACT.split_record(
                record.read_text(encoding="utf-8")
            )

        self.assertEqual(before_body, after_body)
        self.assertEqual(
            {"normalized_format"}, set(after_frontmatter) - set(before_frontmatter)
        )
        self.assertEqual(set(), set(before_frontmatter) - set(after_frontmatter))
        # `normalized_at` records when the record was written, so a rewrite is supposed
        # to move it. It is second-resolution, so comparing it asserts only that the two
        # runs landed in the same second — which passes locally and fails on a slow
        # machine. Excluded here and checked on its own terms below.
        self.assertEqual(
            [],
            [
                key
                for key in before_frontmatter
                if key != "normalized_at"
                and before_frontmatter[key] != after_frontmatter[key]
            ],
        )
        self.assertGreaterEqual(after_frontmatter["normalized_at"], before_frontmatter["normalized_at"])
        self.assertEqual(CONTRACT.NORMALIZED_FORMAT_VERSION, after_frontmatter["normalized_format"])


if __name__ == "__main__":
    unittest.main()
