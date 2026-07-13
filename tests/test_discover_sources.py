"""Tests for the read-only discovery CLI skeleton (E31-T03).

`discover_sources.py` is intentionally inert: it validates arguments, loads
config, enforces the disabled-by-default discovery gate, and emits a structured
error envelope. It must never run network I/O. These tests assert the disabled
refusal, the enabled-but-not-implemented refusal, the JSON envelope shape,
argument validation, and that no command opens a socket.
"""

import contextlib
import importlib.util
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
STANDARDS_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "standards-registry"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_under_test", "discover_sources.py")

# One representative invocation per read-only subcommand, with all required
# arguments supplied so the run reaches the discovery gate.
COMMAND_CASES = {
    "search": ["search", "--query", "retrieval augmented generation"],
    "legal": ["legal", "--jurisdiction", "us-federal", "--topic", "emissions reporting"],
    "github": ["github", "--query", "retrieval augmented generation"],
    "authors": ["authors", "--source-id", "paper:2601.00001v1"],
    "standards": [
        "standards",
        "iso-open-data",
        "--designation",
        "ISO 19131:2022",
        "--fixture",
        str(STANDARDS_FIXTURES / "iso-open-data-deliverables.jsonl"),
    ],
}

# Every discovery provider transport now has a real adapter: `github` (E32-T02),
# `search` (E33-T01), `legal` (E34-T02), `authors` (E35-T01), and fixture-backed
# `standards` (SRSE-004..007). Their enabled-path behavior is covered by their
# own focused tests, so there are no inert provider commands left to assert here.
INERT_COMMAND_CASES = {
    name: args
    for name, args in COMMAND_CASES.items()
    if name not in ("github", "search", "legal", "authors", "standards")
}


def write_workspace(
    root: Path,
    *,
    discovery_enabled: bool | None,
    discovery_providers: list[str] | None = None,
) -> Path:
    """Write a minimal research.yml. `None` omits integrations.discovery."""
    target = root / "workspace"
    target.mkdir(parents=True, exist_ok=True)
    lines = ["project:", "  name: discovery-cli-fixture", "integrations:", "  obsidian:", "    enabled: false"]
    if discovery_enabled is not None:
        lines += ["  discovery:", f"    enabled: {'true' if discovery_enabled else 'false'}"]
        if discovery_providers is not None:
            lines += ["    providers:"]
            lines += [f"      - {provider}" for provider in discovery_providers]
    (target / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


class DiscoverSourcesCliTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def json_error(self, stderr: str) -> dict:
        return json.loads(stderr)

    def test_candidate_lifecycle_contract_declares_all_states_and_only_known_transition_targets(self):
        expected_states = {
            "proposed",
            "reviewed",
            "selected",
            "rejected",
            "deferred",
            "fetched",
            "failed",
            "superseded",
        }

        self.assertEqual(expected_states, set(DISCOVER.CANDIDATE_LIFECYCLE_STATES))
        self.assertEqual(expected_states, set(DISCOVER.CANDIDATE_STATE_TRANSITIONS))
        self.assertEqual(
            set(),
            {
                target
                for targets in DISCOVER.CANDIDATE_STATE_TRANSITIONS.values()
                for target in targets
            }
            - expected_states,
        )
        self.assertEqual((), DISCOVER.CANDIDATE_STATE_TRANSITIONS["rejected"])
        self.assertEqual((), DISCOVER.CANDIDATE_STATE_TRANSITIONS["fetched"])
        self.assertEqual((), DISCOVER.CANDIDATE_STATE_TRANSITIONS["superseded"])

    # --- disabled-by-default gate ---------------------------------------

    def test_disabled_by_default_when_discovery_block_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=None)
            for name, command_args in COMMAND_CASES.items():
                with self.subTest(command=name):
                    code, stdout, stderr = self.run_cli(
                        ["--project-root", str(target), "--format", "json", *command_args]
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    envelope = self.json_error(stderr)
                    self.assertEqual("DISCOVERY_DISABLED", envelope["error_code"])
                    self.assertEqual(name, envelope["details"]["command"])
                    self.assertIs(False, envelope["details"]["network_io_executed"])

    def test_disabled_when_enabled_is_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=False)
            code, _, stderr = self.run_cli(
                ["--project-root", str(target), "--format", "json", *COMMAND_CASES["search"]]
            )
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", self.json_error(stderr)["error_code"])

    # --- inert-but-enabled gate -----------------------------------------

    def test_enabled_discovery_returns_not_implemented(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=True)
            for name, command_args in INERT_COMMAND_CASES.items():
                with self.subTest(command=name):
                    code, stdout, stderr = self.run_cli(
                        ["--project-root", str(target), "--format", "json", *command_args]
                    )
                    self.assertEqual(2, code)
                    self.assertEqual("", stdout)
                    envelope = self.json_error(stderr)
                    self.assertEqual("NOT_IMPLEMENTED", envelope["error_code"])
                    self.assertEqual(name, envelope["details"]["command"])
                    self.assertIs(False, envelope["details"]["network_io_executed"])

    # --- envelope shape --------------------------------------------------

    def test_json_envelope_has_stable_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=None)
            _, _, stderr = self.run_cli(
                ["--project-root", str(target), "--format", "json", *COMMAND_CASES["search"]]
            )
        envelope = self.json_error(stderr)
        self.assertEqual("1.0", envelope["schema_version"])
        self.assertEqual("DISCOVERY_DISABLED", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        self.assertIn("discovery", envelope["message"].lower())
        self.assertIn("integrations.discovery.enabled", envelope["remediation"])

    def test_text_mode_emits_plain_message_not_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=None)
            code, stdout, stderr = self.run_cli(
                ["--project-root", str(target), *COMMAND_CASES["search"]]
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("Discovery is disabled", stderr)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(stderr)

    # --- argument validation --------------------------------------------

    def test_empty_required_value_is_value_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=True)
            code, _, stderr = self.run_cli(
                ["--project-root", str(target), "--format", "json", "search", "--query", "x", "--request-id", "   "]
            )
        self.assertEqual(2, code)
        self.assertEqual("VALUE_INVALID", self.json_error(stderr)["error_code"])

    def test_missing_required_argument_exits_via_argparse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=True)
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    DISCOVER.main(["--project-root", str(target), "--format", "json", "search"])
        self.assertEqual(2, ctx.exception.code)

    def test_non_positive_max_results_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = write_workspace(Path(tmpdir), discovery_enabled=True)
            with self.assertRaises(SystemExit) as ctx:
                with contextlib.redirect_stderr(io.StringIO()):
                    DISCOVER.main(
                        ["--project-root", str(target), "search", "--query", "x", "--max-results", "0"]
                    )
        self.assertEqual(2, ctx.exception.code)

    # --- missing config --------------------------------------------------

    def test_missing_config_is_config_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            empty = Path(tmpdir) / "no-workspace"
            empty.mkdir()
            code, _, stderr = self.run_cli(
                ["--project-root", str(empty), "--format", "json", *COMMAND_CASES["search"]]
            )
        self.assertEqual(2, code)
        self.assertEqual("CONFIG_MISSING", self.json_error(stderr)["error_code"])

    # --- no network I/O --------------------------------------------------

    def test_no_command_opens_a_socket(self):
        def forbid_socket(*args, **kwargs):  # pragma: no cover - only fires on a bug
            raise AssertionError("discovery must not open a network socket")

        original = socket.socket
        socket.socket = forbid_socket
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                disabled = write_workspace(Path(tmpdir) / "off", discovery_enabled=None)
                enabled = write_workspace(Path(tmpdir) / "on", discovery_enabled=True)
                # Disabled discovery refuses every command before any network I/O,
                # including github. Enabled discovery still refuses the inert
                # commands without a socket; github's enabled network path uses an
                # injected transport in test_discover_github.py instead.
                cases = [(disabled, command_args) for command_args in COMMAND_CASES.values()]
                cases += [(enabled, command_args) for command_args in INERT_COMMAND_CASES.values()]
                for target, command_args in cases:
                    code, _, _ = self.run_cli(
                        ["--project-root", str(target), "--format", "json", *command_args]
                    )
                    self.assertEqual(2, code)
        finally:
            socket.socket = original


class StandardsDiscoveryFixtureTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def enabled_workspace(self, root: Path) -> Path:
        return write_workspace(root, discovery_enabled=True, discovery_providers=["standards"])

    def run_standards(self, workspace: Path, *args: str) -> dict:
        code, stdout, stderr = self.run_cli(
            ["--project-root", str(workspace), "--format", "json", "standards", *args]
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def test_iso_open_data_fixture_emits_exact_standards_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            report = self.run_standards(
                workspace,
                "iso-open-data",
                "--designation",
                "ISO 19131:2022",
                "--fixture",
                str(STANDARDS_FIXTURES / "iso-open-data-deliverables.jsonl"),
                "--ics-fixture",
                str(STANDARDS_FIXTURES / "iso-open-data-ics.csv"),
                "--attribution-fixture",
                str(STANDARDS_FIXTURES / "iso-open-data-attribution.json"),
                "--max-results",
                "5",
            )

        self.assertEqual("standards", report["provider"])
        self.assertEqual("iso-open-data", report["standards_provider"])
        self.assertFalse(report["network_io_executed"])
        candidates = report["candidates"]
        self.assertEqual(1, sum(c["standards"]["designation"] == "ISO 19131:2022" for c in candidates))
        exact = next(c for c in candidates if c["standards"]["designation"] == "ISO 19131:2022")
        self.assertEqual("fetch", exact["recommended_action"])
        self.assertEqual("standards_registry_entry", exact["source_type"])
        self.assertEqual("official_standards_registry", exact["source_policy"])
        self.assertEqual("Geographic information - Data product specifications", exact["standards"]["title"])
        self.assertEqual(["35.240.70"], exact["standards"]["ics_codes"])
        self.assertEqual("IT applications in science", exact["standards"]["ics_titles"]["35.240.70"])
        self.assertEqual("ODC-BY-1.0", exact["standards"]["dataset_license"])
        self.assertTrue(exact["standards"]["attribution_required"])
        self.assertEqual("proposed", exact["lifecycle_state"])
        self.assertEqual("new", exact["status"])
        self.assertFalse(exact["network_io_executed"])

    def test_iso_wrong_edition_and_draft_records_are_review_or_reject(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            report = self.run_standards(
                workspace,
                "iso-open-data",
                "--query",
                "geographic information data product",
                "--fixture",
                str(STANDARDS_FIXTURES / "iso-open-data-deliverables.jsonl"),
                "--max-results",
                "5",
            )

        by_designation = {candidate["standards"]["designation"]: candidate for candidate in report["candidates"]}
        self.assertEqual("review", by_designation["ISO 19131:2007"]["recommended_action"])
        self.assertIn("standard_status_withdrawn", by_designation["ISO 19131:2007"]["reasoning"]["risk_flags"])
        self.assertEqual("reject", by_designation["ISO/DIS 19131"]["recommended_action"])
        self.assertIn("standard_status_draft", by_designation["ISO/DIS 19131"]["reasoning"]["risk_flags"])

    def test_replaced_standard_status_uses_superseded_risk_label(self):
        action, flags = DISCOVER.standards_status_action("replaced", exact=True, terms_known=True)

        self.assertEqual("reject", action)
        self.assertIn("standard_status_superseded", flags)
        self.assertNotIn("standard_status_withdrawn", flags)

    def test_superseded_official_legal_page_is_retained_but_rejected_as_current_evidence(self):
        candidate = {
            "title": "Clean Air Act - REPEALED prior version",
            "url": "https://www.federalregister.gov/old/clean-air-act-1970",
            "trust_tier": "official_primary",
            "official_source": True,
            "recommended_action": "fetch",
            "search": {
                "host": "federalregister.gov",
                "snippet": "This historical version has been superseded.",
            },
            "reasoning": {
                "risk_flags": [],
                "freshness_reason": "Official publication currentness must be verified.",
                "authority_reason": "Official federal publication.",
            },
            "rationale": "",
        }

        refined = DISCOVER.refine_legal_candidates(
            [candidate],
            official_domains=["federalregister.gov"],
            jurisdiction="us-federal",
        )[0]

        self.assertEqual("official_primary", refined["trust_tier"])
        self.assertEqual("reject", refined["recommended_action"])
        self.assertIn("superseded_or_historical", refined["reasoning"]["risk_flags"])
        self.assertIn("recommended_action reject", refined["rationale"])

    def test_eu_product_requirements_fixture_splits_guidance_registry_and_legal_authority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            report = self.run_standards(
                workspace,
                "eu-product-requirements",
                "--query",
                "machinery",
                "--guidance-fixture",
                str(STANDARDS_FIXTURES / "eu-product-requirements.html"),
                "--harmonised-fixture",
                str(STANDARDS_FIXTURES / "eu-harmonised-standards.html"),
                "--ojeu-fixture",
                str(STANDARDS_FIXTURES / "eu-ojeu-reference.json"),
            )

        types = [candidate["source_type"] for candidate in report["candidates"]]
        self.assertIn("product_requirement_guidance", types)
        self.assertIn("harmonised_standard_reference", types)
        self.assertIn("official_legal", types)
        guidance = next(c for c in report["candidates"] if c["source_type"] == "product_requirement_guidance")
        self.assertEqual("review", guidance["recommended_action"])
        self.assertIn("product_requirement_guidance_not_legal_authority", guidance["reasoning"]["risk_flags"])
        legal = next(c for c in report["candidates"] if c["source_type"] == "official_legal")
        self.assertEqual("fetch", legal["recommended_action"])
        self.assertEqual("Regulation (EU) 2023/1230", legal["standards"]["legal_act"])

    def test_uk_geospatial_register_fixture_preserves_governance_and_owner_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            report = self.run_standards(
                workspace,
                "uk-geospatial-register",
                "--query",
                "geospatial standards",
                "--fixture",
                str(STANDARDS_FIXTURES / "uk-geospatial-register.html"),
            )

        self.assertGreaterEqual(len(report["candidates"]), 3)
        iso = next(c for c in report["candidates"] if c["standards"]["designation"] == "ISO 19131:2022")
        self.assertEqual("geospatial_standard_register_entry", iso["source_type"])
        self.assertEqual("Geospatial Commission", iso["standards"]["register_owner"])
        self.assertEqual("Ordnance Survey", iso["standards"]["register_manager"])
        self.assertEqual("BSI IST/36", iso["standards"]["control_body"])
        self.assertEqual("ISO", iso["standards"]["linked_owner_references"][0]["owner"])
        self.assertIn("underlying_standard_identity_requires_owner_record", iso["reasoning"]["risk_flags"])

    def test_nist_fixture_distinguishes_guidance_from_concrete_publication(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            report = self.run_standards(
                workspace,
                "nist",
                "--query",
                "FIPS 140-3",
                "--guidance-fixture",
                str(STANDARDS_FIXTURES / "nist-standards-information-center.html"),
                "--publication-fixture",
                str(STANDARDS_FIXTURES / "nist-csrc-publication.json"),
            )

        guidance = next(c for c in report["candidates"] if c["provider"] == "nist-standards-info")
        publication = next(c for c in report["candidates"] if c["provider"] == "nist-csrc")
        self.assertEqual("review", guidance["recommended_action"])
        self.assertIn("nist_guidance_not_publication_identity", guidance["reasoning"]["risk_flags"])
        self.assertEqual("fetch", publication["recommended_action"])
        self.assertEqual("FIPS 140-3", publication["standards"]["designation"])
        self.assertEqual("NIST", publication["standards"]["standards_body"])

    def test_standards_live_run_without_fixture_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.enabled_workspace(Path(tmpdir))
            code, stdout, stderr = self.run_cli(
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "standards",
                    "iso-open-data",
                    "--designation",
                    "ISO 19131:2022",
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("PROVIDER_FAILED", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        self.assertFalse(envelope["details"]["network_io_executed"])

    def test_standards_requires_explicit_discovery_provider_allowlist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = write_workspace(Path(tmpdir), discovery_enabled=True, discovery_providers=[])
            code, stdout, stderr = self.run_cli(
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "standards",
                    "iso-open-data",
                    "--designation",
                    "ISO 19131:2022",
                    "--fixture",
                    str(STANDARDS_FIXTURES / "iso-open-data-deliverables.jsonl"),
                ]
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("DISCOVERY_PROVIDER_DISABLED", envelope["error_code"])
        self.assertIn("standards", envelope["remediation"])


if __name__ == "__main__":
    unittest.main()
