"""External normalizer adapters: transport, protocol validation, and the record they produce.

Configuring an adapter authorizes this package to execute a command, and the command's
output becomes evidence. The output is therefore treated as untrusted input: a run that
cannot produce a result the package fully understands must fail the action rather than
write a partial or stub record, because a record that exists is trusted by the reopen
gate, by grounding, and by lint.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
STUB_ADAPTER = REPO_ROOT / "tests" / "fixtures" / "normalizer-adapter" / "stub_adapter.py"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADAPTER = load_script_module("research_normalizer_adapter", "_normalizer_adapter.py")
CONFIG = load_script_module("adapter_tests_normalization_config", "_normalization_config.py")
INVENTORY = load_script_module("adapter_tests_inventory", "source_inventory.py")
NORMALIZE = load_script_module("adapter_tests_normalize", "normalize_sources.py")
CONTRACT = load_script_module("adapter_tests_contract", "_normalized_contract.py")

SOURCE_ID = "raw:raw-data-keepa-40efe41f3b"
RECORD_NAME = "raw--raw-data-keepa-40efe41f3b.md"
PAYLOAD = '{"supplier_quote": "23.99 EUR", "price_history": "90d median 21.40 EUR"}\n'


def adapter_config(**overrides):
    declaration = {
        "kinds": ["structured_data"],
        "provider": "command",
        "command": [sys.executable, str(STUB_ADAPTER)],
        "name": "stub-normalize",
        "version": "1.0.0",
    }
    declaration.update(overrides)
    return declaration


@contextlib.contextmanager
def stub_environment(**values: str):
    """Steer the stub adapter's behaviour for one call."""
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, previous in original.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


DEFAULT_ADAPTER = object()


class AdapterWorkspaceMixin:
    """Workspace scaffolding shared by the adapter test classes.

    Deliberately not a TestCase: subclassing one that carries tests would re-run the
    whole parent suite under every child class.
    """

    def make_workspace(self, root: Path, *, adapter: dict | None = DEFAULT_ADAPTER) -> Path:
        if adapter is DEFAULT_ADAPTER:
            adapter = adapter_config()
        workspace = root / "adapter-workspace"
        (workspace / "raw" / "data").mkdir(parents=True)
        (workspace / "sources").mkdir(parents=True)
        config = {
            "raw": {"source_roots": ["raw/data"]},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
            },
        }
        if adapter is not None:
            config["normalization"] = {"adapters": [adapter]}
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        (workspace / "raw" / "data" / "keepa.json").write_text(PAYLOAD, encoding="utf-8")
        (workspace / "raw" / "data" / "keepa.json.provenance.yml").write_text(
            "origin_url: https://api.keepa.com/product/B0ABC\n"
            "license: CC-BY-4.0\n"
            "retrieved_at: 2026-08-08T12:00:00Z\n"
            "retrieved_by: autoseller/keepa\n",
            encoding="utf-8",
        )
        self.inventory(workspace)
        return workspace

    def inventory(self, workspace: Path) -> None:
        config = INVENTORY.load_config(workspace)
        manifest_path = workspace / "sources" / "manifest.jsonl"
        records, _, _ = INVENTORY.build_records(
            workspace, config, previous_detected_at=INVENTORY.existing_detected_at(manifest_path)
        )
        INVENTORY.write_manifest(manifest_path, records)

    def normalize(self, workspace: Path, *extra: str) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(["--project-root", str(workspace), "--all", "--format", "json", *extra])
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def record_path(self, workspace: Path) -> Path:
        return workspace / "sources" / "normalized" / RECORD_NAME

    def frontmatter(self, workspace: Path) -> dict:
        frontmatter, _, _ = CONTRACT.split_record(self.record_path(workspace).read_text(encoding="utf-8"))
        return frontmatter


class AdapterWorkspaceTests(AdapterWorkspaceMixin, unittest.TestCase):
    # -- the happy path ----------------------------------------------------------

    def test_adapter_produces_a_contract_valid_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            code, report, stderr = self.normalize(workspace)

            self.assertEqual(0, code, stderr)
            self.assertEqual(1, report["summary"]["methods"]["adapter"])
            self.assertEqual("created", report["actions"][0]["action"])
            self.assertEqual("content_extracted", report["actions"][0]["status"])

            manifest = {
                record["id"]: record
                for record in (
                    json.loads(line)
                    for line in (workspace / "sources" / "manifest.jsonl").read_text().splitlines()
                    if line.strip()
                )
            }
            violations = CONTRACT.validate_record(
                self.record_path(workspace),
                manifest_by_id=manifest,
                normalized_root=workspace / "sources" / "normalized",
            )

        self.assertEqual([], violations, [v.message for v in violations])

    def test_record_names_the_adapter_as_its_producer(self):
        # Staleness and lint both key on this identity; the record must not claim to
        # have come from this package when an adapter produced it.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize(workspace)
            frontmatter = self.frontmatter(workspace)

        self.assertEqual({"name": "stub-normalize", "version": "1.0.0"}, frontmatter["normalizer"])
        self.assertEqual("adapter", frontmatter["extraction_method"])
        self.assertEqual("structured_data", frontmatter["source_kind"])
        self.assertFalse(CONTRACT.is_native_record(frontmatter))

    def test_package_owned_fields_come_from_the_manifest_not_the_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize(workspace)
            frontmatter = self.frontmatter(workspace)
            manifest_record = json.loads(
                (workspace / "sources" / "manifest.jsonl").read_text().splitlines()[0]
            )

        self.assertEqual(manifest_record["raw_fingerprint"], frontmatter["raw_fingerprint"])
        self.assertEqual(["raw/data/keepa.json"], frontmatter["raw_paths"])
        self.assertEqual(1, frontmatter["normalized_format"])
        self.assertTrue(frontmatter["content_hash"].startswith("sha256:"))
        self.assertEqual("https://api.keepa.com/product/B0ABC", frontmatter["provenance"]["origin_url"])

    def test_facet_headings_are_quotable(self):
        # The point of the whole path: a value inside a facet section can ground a claim.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize(workspace)
            body = self.record_path(workspace).read_text(encoding="utf-8")

        self.assertIn("### supplier_quote", body)
        self.assertIn("supplier_quote: 23.99 EUR", body)
        # Only the record's own eight sections may be level two.
        self.assertEqual(CONTRACT.REQUIRED_SECTIONS, tuple(CONTRACT.section_order(body)))

    def test_adapter_reported_partial_status_is_kept(self):
        # The body looks complete either way; only the adapter knows it capped content.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_STATUS="partial"):
                code, report, stderr = self.normalize(workspace)

            self.assertEqual(0, code, stderr)
            self.assertEqual("partial", report["actions"][0]["status"])
            self.assertEqual(1, report["summary"]["partial"])
            self.assertEqual("partial", self.frontmatter(workspace)["status"])

    def test_adapter_warnings_reach_the_record_and_the_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_WARNINGS='["price series capped to 90 days"]'):
                _, report, _ = self.normalize(workspace)
            frontmatter = self.frontmatter(workspace)
            body = self.record_path(workspace).read_text(encoding="utf-8")

        self.assertEqual(["price series capped to 90 days"], frontmatter["parse_warnings"])
        self.assertIn("price series capped to 90 days", body)
        self.assertIn("price series capped to 90 days", report["actions"][0]["warnings"])

    def test_adapter_stderr_is_captured_as_a_warning_not_parsed_as_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_MODE="stderr_noise"):
                code, report, _ = self.normalize(workspace)

            self.assertEqual(0, code)
            self.assertTrue(
                any("progress: starting" in warning for warning in report["actions"][0]["warnings"]),
                report["actions"][0]["warnings"],
            )

    # -- no adapter configured ---------------------------------------------------

    def test_without_an_adapter_the_kind_stays_classified_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=None)
            code, report, stderr = self.normalize(workspace)

            self.assertEqual(0, code, stderr)
            self.assertEqual(0, report["summary"]["methods"]["adapter"])
            self.assertEqual(1, report["summary"]["skipped_unsupported"])
            self.assertFalse(self.record_path(workspace).exists())

    def test_an_unmapped_kind_is_not_routed_to_a_configured_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=adapter_config(kinds=["sensor_series"]))
            code, report, stderr = self.normalize(workspace)

            self.assertEqual(0, code, stderr)
            self.assertEqual(0, report["summary"]["methods"]["adapter"])
            self.assertFalse(self.record_path(workspace).exists())

    # -- failure paths: every one must leave no record behind ---------------------

    def assertFailedAction(self, workspace: Path, *, contains: str, env: dict[str, str]):
        with stub_environment(**env):
            code, report, _ = self.normalize(workspace)
        self.assertEqual(1, code, "a failed adapter run must exit non-zero")
        action = report["actions"][0]
        self.assertEqual("failed", action["action"])
        self.assertIn(contains, action["error"])
        self.assertFalse(self.record_path(workspace).exists(), "a failed run must write no record")
        self.assertEqual(1, report["summary"]["failed"])

    def test_non_json_stdout_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(workspace, contains="not JSON", env={"EW_STUB_MODE": "garbage"})

    def test_a_second_stdout_document_fails_closed(self):
        # An adapter that also logs to stdout must not have its diagnostics folded into
        # evidence, nor scanned past.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(
                workspace, contains="more than one document", env={"EW_STUB_MODE": "trailing"}
            )

    def test_stdout_noise_before_the_document_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(workspace, contains="not JSON", env={"EW_STUB_MODE": "stdout_noise"})

    def test_non_zero_exit_fails_closed_and_reports_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(
                workspace, contains="stub adapter refused to run", env={"EW_STUB_MODE": "nonzero"}
            )

    def test_identity_mismatch_fails_closed(self):
        # The workspace authorized a specific tool and version; anything else is either
        # a misconfiguration or not what was reviewed.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(
                workspace, contains="research.yml authorized", env={"EW_STUB_VERSION": "9.9.9"}
            )

    def test_level_two_heading_in_the_body_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.assertFailedAction(
                workspace, contains="level-two heading", env={"EW_STUB_MODE": "level_two_heading"}
            )

    def test_timeout_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=adapter_config(timeout_seconds=1))
            self.assertFailedAction(workspace, contains="timed out after 1s", env={"EW_STUB_MODE": "hang"})

    def test_missing_executable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir), adapter=adapter_config(command=["definitely-not-on-path-ew"])
            )
            self.assertFailedAction(workspace, contains="could not be executed", env={})


class AdapterRenderedCoverageTests(AdapterWorkspaceMixin, unittest.TestCase):
    """A capped rendering must say so in the record it produces.

    Grounding is by containment against the body, so content the renderer dropped is
    citable but never quotable. Without these counts that loss is invisible — the record
    looks complete from the outside.
    """

    def test_coverage_reaches_the_record_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize(workspace)
            coverage = self.frontmatter(workspace)["rendered_coverage"]

        self.assertEqual(2, coverage["total_values"])
        self.assertEqual(2, coverage["rendered_values"])
        self.assertEqual(1.0, coverage["ratio"])
        self.assertEqual(
            {"supplier_quote", "price_history"},
            {section["heading"] for section in coverage["sections"]},
        )

    def test_a_capped_rendering_reports_a_partial_ratio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_CAP="1"):
                code, _, stderr = self.normalize(workspace)
            self.assertEqual(0, code, stderr)
            coverage = self.frontmatter(workspace)["rendered_coverage"]
            body = self.record_path(workspace).read_text(encoding="utf-8")

        self.assertEqual(2, coverage["total_values"])
        self.assertEqual(1, coverage["rendered_values"])
        self.assertEqual(0.5, coverage["ratio"])
        dropped = [section for section in coverage["sections"] if section["rendered"] == 0]
        self.assertEqual(1, len(dropped))
        self.assertIn("note", dropped[0], "a cap must be recorded as a section note")
        # The dropped facet really is unquotable: its value is not in the body.
        self.assertNotIn("90d median 21.40 EUR", body)

    def test_an_adapter_that_omits_coverage_fails_the_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_MODE="no_coverage"):
                code, report, _ = self.normalize(workspace)

            self.assertEqual(1, code)
            self.assertIn("rendered_coverage", report["actions"][0]["error"])
            self.assertFalse(self.record_path(workspace).exists())

    def test_an_incoherent_coverage_claim_fails_the_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            with stub_environment(EW_STUB_MODE="bad_coverage"):
                code, report, _ = self.normalize(workspace)

            self.assertEqual(1, code)
            self.assertIn("invalid `rendered_coverage`", report["actions"][0]["error"])
            self.assertFalse(self.record_path(workspace).exists())


class AdapterStalenessTests(AdapterWorkspaceMixin, unittest.TestCase):
    """When an adapter re-runs, and — just as importantly — when it does not.

    An adapter record is versioned by its adapter, not by this script. Comparing it to
    NORMALIZER_VERSION would mark every adapter record stale on every run, re-executing
    a subprocess to reproduce a record the workspace already had.
    """

    def pending_run(self, workspace: Path, **env: str) -> dict:
        with stub_environment(**env):
            _, report, _ = self.normalize_pending(workspace)
        return report

    def normalize_pending(self, workspace: Path, *extra: str) -> tuple[int, dict, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(["--project-root", str(workspace), "--format", "json", *extra])
        raw = stdout.getvalue()
        return int(code or 0), (json.loads(raw) if raw.strip() else {}), stderr.getvalue()

    def set_adapter_version(self, workspace: Path, version: str) -> None:
        config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
        config["normalization"]["adapters"][0]["version"] = version
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def test_an_unchanged_record_does_not_re_run_the_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))

            first = self.pending_run(workspace)
            self.assertEqual("created", first["actions"][0]["action"])

            for run in range(2):
                with self.subTest(run=run):
                    report = self.pending_run(workspace)
                    self.assertEqual([], report["actions"], "adapter re-ran with nothing changed")
                    self.assertEqual(0, report["summary"]["planned"])

    def test_all_reports_an_unchanged_adapter_record_as_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize(workspace)

            with stub_environment():
                _, report, _ = self.normalize(workspace)

            self.assertEqual("skipped_existing", report["actions"][0]["action"])
            self.assertFalse(report["actions"][0]["stale"])
            self.assertEqual(1, report["summary"]["skipped_existing"])

    def test_a_new_adapter_version_makes_the_record_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.pending_run(workspace)

            self.set_adapter_version(workspace, "1.1.0")
            report = self.pending_run(workspace, EW_STUB_VERSION="1.1.0")

            self.assertEqual("updated", report["actions"][0]["action"])
            self.assertTrue(report["actions"][0]["stale"])
            self.assertEqual("1.1.0", self.frontmatter(workspace)["normalizer"]["version"])

            # And settles again: the bump is a one-time trigger, not a permanent one.
            settled = self.pending_run(workspace, EW_STUB_VERSION="1.1.0")
            self.assertEqual([], settled["actions"])

    def test_a_new_adapter_name_makes_the_record_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.pending_run(workspace)

            config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
            config["normalization"]["adapters"][0]["name"] = "other-normalize"
            (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

            report = self.pending_run(workspace, EW_STUB_NAME="other-normalize")
            self.assertEqual("updated", report["actions"][0]["action"])
            self.assertEqual("other-normalize", self.frontmatter(workspace)["normalizer"]["name"])

    def test_a_changed_payload_makes_the_record_stale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.pending_run(workspace)

            (workspace / "raw" / "data" / "keepa.json").write_text(
                '{"supplier_quote": "24.99 EUR"}\n', encoding="utf-8"
            )
            self.inventory(workspace)

            report = self.pending_run(workspace)
            self.assertEqual("updated", report["actions"][0]["action"])
            self.assertTrue(report["actions"][0]["stale"])
            self.assertIn("24.99 EUR", self.record_path(workspace).read_text(encoding="utf-8"))

    def test_a_string_adapter_version_is_compared_as_written(self):
        # The stored version is the adapter's own (`"1.0.0"`), not this script's integer.
        # Reading it through the native integer path yields None and would look stale.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.pending_run(workspace)
            frontmatter = self.frontmatter(workspace)

            self.assertEqual("1.0.0", frontmatter["normalizer"]["version"])
            self.assertIsNone(NORMALIZE.stored_normalizer_version(frontmatter))
            self.assertEqual(
                ("stub-normalize", "1.0.0"),
                NORMALIZE.stored_normalizer_identity(frontmatter),
            )

    def test_native_records_still_use_the_normalizer_version_axis(self):
        # The adapter rule must not loosen staleness for records this package produces.
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "rec.md"
            output.write_text(
                "---\nnormalizer:\n  name: normalize_sources.py\n"
                f"  version: {NORMALIZE.NORMALIZER_VERSION}\n---\n# x\n",
                encoding="utf-8",
            )
            self.assertFalse(NORMALIZE.is_stale({}, output))

            output.write_text(
                "---\nnormalizer:\n  name: normalize_sources.py\n  version: 1\n---\n# x\n",
                encoding="utf-8",
            )
            self.assertTrue(NORMALIZE.is_stale({}, output))

    def test_a_hand_written_record_for_an_unmapped_kind_is_never_regenerated(self):
        # No adapter for the kind means the record is not eligible at all, so even
        # `--all --force` leaves it alone. This is the safe place to hand-write records.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=None)
            record_path = self.record_path(workspace)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text("HANDWRITTEN-MARKER\n", encoding="utf-8")

            code, report, stderr = self.normalize(workspace, "--force")

            self.assertEqual(0, code, stderr)
            self.assertEqual([], report["actions"])
            self.assertEqual("HANDWRITTEN-MARKER\n", record_path.read_text(encoding="utf-8"))

    def test_a_hand_written_record_for_a_mapped_kind_is_replaced_by_the_adapter(self):
        # Configuring an adapter for a kind hands that kind to the adapter. The record
        # claims a producer the workspace did not authorize, so it is stale.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            record_path = self.record_path(workspace)
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                "---\nnormalizer:\n  name: someone-else\n  version: 9\n---\n\nHANDWRITTEN-MARKER\n",
                encoding="utf-8",
            )

            report = self.pending_run(workspace)

            self.assertEqual("updated", report["actions"][0]["action"])
            self.assertNotIn("HANDWRITTEN-MARKER", record_path.read_text(encoding="utf-8"))


class AdapterConfigFailureTests(AdapterWorkspaceMixin, unittest.TestCase):
    """A misconfigured adapter section is a fatal setup error, reported like one.

    It is read before any record is selected, so it cannot become a per-source failed
    action. Without explicit handling it escaped as a traceback, leaving a host with no
    error code and stdout that parses as nothing.
    """

    def test_invalid_section_emits_the_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(
                Path(tmpdir), adapter=adapter_config(command="autoseller-normalize --format json")
            )

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = NORMALIZE.main(
                    ["--project-root", str(workspace), "--all", "--format", "json"]
                )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue(), "a fatal setup error must leave stdout empty")
        envelope = json.loads(stderr.getvalue())
        self.assertEqual("CONFIG_INVALID", envelope["error_code"])
        self.assertIn("must be a list of arguments", envelope["message"])
        self.assertIn("docs/research-yml.md", envelope["remediation"])

    def test_invalid_section_in_text_mode_reports_the_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir), adapter=adapter_config(provider="http"))

            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = NORMALIZE.main(["--project-root", str(workspace), "--all"])

        self.assertEqual(2, code)
        self.assertIn("provider", stderr.getvalue())


class AdapterReportingTests(AdapterWorkspaceMixin, unittest.TestCase):
    """Adapter work must be visible in every place normalization reports itself.

    A run that silently does subprocess work is one an operator cannot audit, so the
    method counters, the stderr summary, the activity log, and the per-action record
    all have to name it.
    """

    def normalize_text(self, workspace: Path, *extra: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = NORMALIZE.main(["--project-root", str(workspace), "--all", *extra])
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_json_report_counts_adapter_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            _, report, _ = self.normalize(workspace)

        self.assertEqual(1, report["summary"]["methods"]["adapter"])
        # Every extractor keeps a key, so a consumer iterating methods sees a stable set.
        self.assertEqual(
            {"latex", "pdf", "links", "html", "tables", "codebase", "adapter"},
            set(report["summary"]["methods"]),
        )

    def test_action_records_name_the_producing_adapter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            _, report, _ = self.normalize(workspace)

        self.assertEqual({"name": "stub-normalize", "version": "1.0.0"}, report["actions"][0]["adapter"])

    def test_non_adapter_actions_report_no_adapter(self):
        # A natively normalized record alongside the adapter one: the `adapter` field
        # must distinguish them rather than being stamped on everything.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            (workspace / "raw" / "data" / "rows.csv").write_text("sku,price\nB0ABC,23.99\n", encoding="utf-8")
            self.inventory(workspace)
            _, report, _ = self.normalize(workspace)

        by_method = {action["method"]: action for action in report["actions"]}
        self.assertEqual({"adapter", "table"}, set(by_method), report["actions"])
        self.assertEqual({"name": "stub-normalize", "version": "1.0.0"}, by_method["adapter"]["adapter"])
        self.assertIsNone(by_method["table"]["adapter"])

    def test_stderr_summary_line_reports_the_adapter_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            code, stdout, stderr = self.normalize_text(workspace)

        self.assertEqual(0, code, stderr)
        summary_line = next(line for line in stderr.splitlines() if line.startswith("summary "))
        self.assertIn("adapter=1", summary_line)

    def test_activity_log_entry_reports_the_adapter_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            self.normalize_text(workspace, "--append-log")
            log = (workspace / "log.md").read_text(encoding="utf-8")

        methods_line = next(line for line in log.splitlines() if line.startswith("- Methods:"))
        self.assertIn("adapter=1", methods_line)

    def test_report_schema_version_is_unchanged(self):
        # New counters are forward-compatible additions. Bumping would break consumers
        # that pin "1.0" while signalling a breaking change that did not happen — the
        # package's own policy is to bump only on breaking shape changes.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.make_workspace(Path(tmpdir))
            _, report, _ = self.normalize(workspace)

        self.assertEqual("1.0", report["schema_version"])
        self.assertEqual("source_normalization_report", report["document_type"])


class AdapterSelfCheckTests(unittest.TestCase):
    """The package holds its own adapter rendering to the record contract.

    Response validation makes a non-conforming rendering hard to reach, which is the
    point — but if one ever is, shipping it would put a broken record into the
    workspace under the package's own rendering. It fails loudly and leaves nothing.
    """

    def test_invalid_rendering_fails_the_action_and_removes_the_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            normalized_root = workspace / "sources" / "normalized"
            normalized_root.mkdir(parents=True)
            record_path = normalized_root / RECORD_NAME
            record_path.write_text("---\ntype: not_a_normalized_source\n---\n\n# broken\n", encoding="utf-8")

            # `normalize_sources` imports its own instance of the adapter module, so
            # assert against the class it actually raises rather than this test's copy.
            with self.assertRaises(NORMALIZE.AdapterError) as caught:
                NORMALIZE.verify_adapter_output(
                    workspace,
                    record_path,
                    [{"id": SOURCE_ID, "kind": "structured_data", "raw_paths": ["raw/data/keepa.json"]}],
                    normalized_root,
                )

            self.assertIn("did not satisfy the record contract", str(caught.exception))
            self.assertIn("NORMALIZED_CONTRACT_", str(caught.exception))
            self.assertFalse(record_path.exists(), "a rejected rendering must not be left on disk")

    def test_conforming_rendering_passes_silently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            normalized_root = workspace / "sources" / "normalized"
            normalized_root.mkdir(parents=True)
            record = {"id": SOURCE_ID, "kind": "structured_data", "raw_paths": ["raw/data/keepa.json"]}
            written, _ = NORMALIZE.write_normalized_source(
                NORMALIZE.NormalizedSource(
                    record=record,
                    extraction_method=ADAPTER.EXTRACTION_METHOD,
                    title="Keepa",
                    authors=[],
                    abstract="Snapshot.",
                    outline=[(3, "supplier_quote")],
                    extracted_text="### supplier_quote\n\n- price: 23.99 EUR\n",
                    media=[],
                    links=[],
                    bibliography_files=[],
                    included_paths=[],
                    warnings=[],
                    rendered_coverage={
                        "total_values": 1,
                        "rendered_values": 1,
                        "ratio": 1.0,
                        "sections": [{"heading": "supplier_quote", "total": 1, "rendered": 1}],
                    },
                    adapter_status="content_extracted",
                    adapter_name="stub-normalize",
                    adapter_version="1.0.0",
                ),
                normalized_root,
                "sources/manifest.jsonl",
                "2026-08-08",
                project_root=workspace,
            )
            NORMALIZE.verify_adapter_output(workspace, written, [record], normalized_root)
            self.assertTrue(written.exists())


class AdapterProtocolTests(unittest.TestCase):
    """Response validation, exercised directly so every rule has a focused case."""

    def adapter(self, **overrides):
        return CONFIG.normalization_config({"normalization": {"adapters": [adapter_config(**overrides)]}})[
            "adapters"
        ][0]

    def valid_payload(self, **overrides):
        payload = {
            "schema_version": "1.0",
            "document_type": "normalizer_adapter_result",
            "adapter": {"name": "stub-normalize", "version": "1.0.0"},
            "status": "content_extracted",
            "body_markdown": "### facet\n\n- value: 1\n",
            "rendered_coverage": {
                "total_values": 1,
                "rendered_values": 1,
                "ratio": 1.0,
                "sections": [{"heading": "facet", "total": 1, "rendered": 1}],
            },
        }
        payload.update(overrides)
        return payload

    def validate(self, payload, **kwargs):
        return ADAPTER.validate_result(payload, adapter=self.adapter(), source_id="raw:x", **kwargs)

    def assertRejected(self, payload, *, contains: str):
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            self.validate(payload)
        self.assertIn(contains, str(caught.exception))

    def test_minimal_valid_payload_is_accepted(self):
        result = self.validate(self.valid_payload())
        self.assertEqual("content_extracted", result.status)
        self.assertEqual("stub-normalize", result.name)
        self.assertEqual((), result.outline)

    def test_unknown_keys_are_rejected(self):
        self.assertRejected(self.valid_payload(surprise=1), contains="unknown keys: surprise")

    def test_x_prefixed_keys_are_tolerated(self):
        self.validate(self.valid_payload(**{"x-trace": "abc"}))

    def test_schema_and_document_type_are_checked(self):
        self.assertRejected(self.valid_payload(schema_version="2.0"), contains="schema_version")
        self.assertRejected(self.valid_payload(document_type="something_else"), contains="document_type")

    def test_status_must_be_known(self):
        self.assertRejected(self.valid_payload(status="done"), contains="expected one of")

    def test_adapter_reported_failure_is_surfaced_with_its_detail(self):
        self.assertRejected(
            self.valid_payload(status="failed", detail="upstream API returned 503"),
            contains="upstream API returned 503",
        )

    def test_empty_body_is_rejected(self):
        self.assertRejected(self.valid_payload(body_markdown="   "), contains="no `body_markdown`")

    def test_outline_entries_are_validated(self):
        self.assertRejected(self.valid_payload(outline="facets"), contains="non-list `outline`")
        self.assertRejected(self.valid_payload(outline=[["3", "x"]]), contains="outline level")
        self.assertRejected(self.valid_payload(outline=[[9, "x"]]), contains="outline level")
        self.assertRejected(self.valid_payload(outline=[[3, "  "]]), contains="no text")
        self.assertEqual(((3, "facet"),), self.validate(self.valid_payload(outline=[[3, "facet"]])).outline)

    def test_warnings_must_be_strings(self):
        self.assertRejected(self.valid_payload(warnings=[1]), contains="non-string warning")

    def test_stderr_is_appended_as_a_warning(self):
        result = self.validate(self.valid_payload(), stderr="disk slow")
        self.assertIn("adapter stderr: disk slow", result.warnings)

    def test_oversized_stdout_is_refused_before_parsing(self):
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            ADAPTER.parse_result_document(
                "x" * (ADAPTER.MAX_RESULT_BYTES + 1), source_id="raw:x", adapter_name="stub"
            )
        self.assertIn("more than", str(caught.exception))

    def test_empty_stdout_is_refused(self):
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            ADAPTER.parse_result_document("  \n", source_id="raw:x", adapter_name="stub")
        self.assertIn("returned nothing", str(caught.exception))

    def test_json_array_is_refused(self):
        with self.assertRaises(ADAPTER.AdapterError) as caught:
            ADAPTER.parse_result_document("[1, 2]", source_id="raw:x", adapter_name="stub")
        self.assertIn("not an object", str(caught.exception))

    def test_request_document_shape(self):
        request = ADAPTER.build_request(
            Path("/ws"), {"id": "raw:x", "kind": "structured_data"}, raw_paths=["raw/data/x.json"], normalized_format=1
        )
        self.assertEqual("normalizer_adapter_request", request["document_type"])
        self.assertEqual("1.0", request["schema_version"])
        self.assertEqual(1, request["normalized_format"])
        self.assertEqual(["raw/data/x.json"], request["raw_paths"])
        self.assertEqual("raw:x", request["manifest_record"]["id"])


if __name__ == "__main__":
    unittest.main()
