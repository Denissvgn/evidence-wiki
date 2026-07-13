"""Tests for the jurisdiction profile schema, loader, validator, and CLI (E34-T01).

`discover_sources.py jurisdictions validate|list|show` reads a workspace-local
`sources/jurisdictions.yml`, validates each profile against the schema, and
exposes official-domain matching helpers that legal discovery (E34-T02/T03) and
the search ranker consume. The command is offline: it reads a YAML file and never
contacts a provider, so it runs even when discovery is disabled.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE_YAML = REPO_ROOT / "tests" / "fixtures" / "discovery" / "jurisdictions.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys = __import__("sys")
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_jurisdictions_under_test", "discover_sources.py")
JurisdictionError = DISCOVER.DiscoverSourcesError


def valid_profile(**overrides) -> dict:
    base: dict = {
        "jurisdiction_id": "us-federal",
        "name": "United States (Federal)",
        "country": "us",
        "official_domains": ["govinfo.gov", "WWW.ECFR.gov"],
        "legislature_urls": ["https://www.congress.gov"],
        "regulator_urls": [],
        "court_urls": [],
        "gazette_urls": [],
        "blocked_domains": ["example.com"],
        "notes": "U.S. federal primary legal sources.",
    }
    base.update(overrides)
    return base


class JurisdictionProfileSchemaTests(unittest.TestCase):
    """Schema validation of individual profiles and whole documents."""

    def test_valid_country_level_profile_normalizes(self):
        profile = DISCOVER.validate_jurisdiction_profile(valid_profile(), set())
        self.assertEqual("us-federal", profile["jurisdiction_id"])
        self.assertEqual("US", profile["country"])
        # Domains are normalized (lowercased, www stripped) and order preserved.
        self.assertEqual(["govinfo.gov", "ecfr.gov"], profile["official_domains"])
        self.assertEqual(["example.com"], profile["blocked_domains"])
        self.assertEqual(["https://www.congress.gov"], profile["legislature_urls"])
        self.assertEqual("1.0", profile["schema_version"])

    def test_valid_state_level_profile_carries_state_or_region(self):
        profile = DISCOVER.validate_jurisdiction_profile(
            valid_profile(
                jurisdiction_id="us-ca",
                name="California (State)",
                state_or_region="CA",
                official_domains=["leginfo.legislature.ca.gov"],
            ),
            set(),
        )
        self.assertEqual("us-ca", profile["jurisdiction_id"])
        self.assertEqual("CA", profile["state_or_region"])

    def test_fixture_file_loads_and_validates(self):
        document = __import__("yaml").safe_load(FIXTURE_YAML.read_text(encoding="utf-8"))
        profiles = DISCOVER.validate_jurisdictions_document(document, "jurisdictions.yml")
        ids = {p["jurisdiction_id"] for p in profiles}
        self.assertEqual({"us-federal", "us-ca"}, ids)
        for profile in profiles:
            self.assertIn(profile["country"], ("US",))
            self.assertTrue(
                any(profile[field] for field in DISCOVER.JURISDICTION_OFFICIAL_ROOT_FIELDS),
                "every fixture profile must have at least one official root",
            )

    def test_missing_jurisdiction_id_is_invalid(self):
        profile = valid_profile()
        del profile["jurisdiction_id"]
        self._assert_invalid(profile, "jurisdiction_id is required")

    def test_bad_slug_is_invalid(self):
        self._assert_invalid(valid_profile(jurisdiction_id="US Federal!"), "lowercase slug")

    def test_duplicate_jurisdiction_id_is_invalid(self):
        seen = set()
        first = DISCOVER.validate_jurisdiction_profile(valid_profile(), seen)
        self.assertIn("us-federal", seen)
        with self.assertRaises(JurisdictionError) as ctx:
            DISCOVER.validate_jurisdiction_profile(valid_profile(), seen)
        self.assertEqual("JURISDICTION_INVALID", ctx.exception.error_code)
        self.assertIn("appears more than once", ctx.exception.message)
        self.assertIsNotNone(first)

    def test_missing_name_is_invalid(self):
        profile = valid_profile()
        del profile["name"]
        self._assert_invalid(profile, "name is required")

    def test_country_must_be_two_letter_iso(self):
        self._assert_invalid(valid_profile(country="USA"), "2-letter")
        self._assert_invalid(valid_profile(country=""), "country is required")

    def test_non_http_url_is_invalid(self):
        self._assert_invalid(
            valid_profile(regulator_urls=["ftp://example.org"]),
            "must be an http(s) URL",
        )

    def test_profile_with_no_official_root_is_invalid(self):
        self._assert_invalid(
            valid_profile(official_domains=[], legislature_urls=[], regulator_urls=[], court_urls=[], gazette_urls=[]),
            "at least one official source root",
        )

    def test_domain_list_wrong_type_is_invalid(self):
        self._assert_invalid(valid_profile(official_domains="govinfo.gov"), "list of domain strings")

    def test_top_level_must_be_mapping(self):
        with self.assertRaises(JurisdictionError) as ctx:
            DISCOVER.validate_jurisdictions_document([], "jurisdictions.yml")
        self.assertEqual("JURISDICTION_INVALID", ctx.exception.error_code)

    def _assert_invalid(self, profile, contains: str) -> None:
        with self.assertRaises(JurisdictionError) as ctx:
            DISCOVER.validate_jurisdiction_profile(profile, set())
        self.assertEqual("JURISDICTION_INVALID", ctx.exception.error_code)
        self.assertIn(contains.lower(), ctx.exception.message.lower())


class JurisdictionMatchingTests(unittest.TestCase):
    """Official-domain matching and lookup helpers consumed by legal discovery."""

    def setUp(self):
        self.profile = DISCOVER.validate_jurisdiction_profile(valid_profile(), set())

    def test_official_domains_flattened(self):
        self.assertEqual(["govinfo.gov", "ecfr.gov"], DISCOVER.profile_official_domains(self.profile))

    def test_exact_host_matches(self):
        self.assertTrue(DISCOVER.profile_matches_host(self.profile, "govinfo.gov"))

    def test_subdomain_matches(self):
        self.assertTrue(DISCOVER.profile_matches_host(self.profile, "www.govinfo.gov"))
        self.assertTrue(DISCOVER.profile_matches_host(self.profile, "api.ecfr.gov"))

    def test_non_official_host_does_not_match(self):
        self.assertFalse(DISCOVER.profile_matches_host(self.profile, "blog.example.com"))

    def test_find_and_require_jurisdiction(self):
        profiles = [self.profile]
        self.assertIs(self.profile, DISCOVER.find_jurisdiction(profiles, "us-federal"))
        self.assertIsNone(DISCOVER.find_jurisdiction(profiles, "us-ca"))
        with self.assertRaises(JurisdictionError) as ctx:
            DISCOVER.require_jurisdiction(profiles, "us-ca")
        self.assertEqual("JURISDICTION_UNKNOWN", ctx.exception.error_code)


class JurisdictionCommandTests(unittest.TestCase):
    """End-to-end through `discover_sources.py jurisdictions ...`."""

    def write_workspace(
        self,
        root: Path,
        *,
        jurisdictions_path: str = "sources/jurisdictions.yml",
        enabled: bool = False,
        content: str | None = None,
    ) -> Path:
        workspace = root / "ws"
        (workspace / "sources").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: jurisdictions-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
        ]
        if jurisdictions_path is not None:
            lines.append(f"    jurisdictions_path: {jurisdictions_path}")
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if jurisdictions_path is not None and content is not None:
            target = workspace / jurisdictions_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return workspace

    def run_cmd(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "jurisdictions", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_validate_succeeds_on_fixture(self):
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=fixture)
            code, stdout, stderr = self.run_cmd(workspace, "validate")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("jurisdictions", report["command"])
        self.assertTrue(report["jurisdictions_path_exists"])
        self.assertEqual(2, report["count"])
        self.assertEqual(["us-federal", "us-ca"], report["jurisdiction_ids"])
        self.assertFalse(report["network_io_executed"])

    def test_list_shows_country_and_state_profiles(self):
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=fixture)
            code, stdout, _ = self.run_cmd(workspace, "list")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        by_id = {j["jurisdiction_id"]: j for j in report["jurisdictions"]}
        self.assertIn("us-federal", by_id)
        self.assertEqual("US", by_id["us-federal"]["country"])
        self.assertEqual("CA", by_id["us-ca"]["state_or_region"])
        self.assertGreater(by_id["us-federal"]["official_root_count"], 0)

    def test_show_returns_full_profile(self):
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=fixture)
            code, stdout, _ = self.run_cmd(workspace, "show", "--jurisdiction", "us-federal")
        self.assertEqual(0, code)
        profile = json.loads(stdout)["jurisdiction"]
        self.assertEqual("us-federal", profile["jurisdiction_id"])
        self.assertEqual("US", profile["country"])
        self.assertIn("govinfo.gov", profile["official_domains"])

    def test_show_unknown_jurisdiction_is_error(self):
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=fixture)
            code, stdout, stderr = self.run_cmd(workspace, "show", "--jurisdiction", "zz-missing")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("JURISDICTION_UNKNOWN", json.loads(stderr)["error_code"])

    def test_missing_file_reports_empty_not_error(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=None)
            code_v, stdout_v, _ = self.run_cmd(workspace, "validate")
            code_l, stdout_l, _ = self.run_cmd(workspace, "list")
        self.assertEqual(0, code_v)
        self.assertFalse(json.loads(stdout_v)["jurisdictions_path_exists"])
        self.assertEqual(0, json.loads(stdout_v)["count"])
        self.assertEqual(0, code_l)
        self.assertEqual(0, json.loads(stdout_l)["count"])

    def test_invalid_profile_is_jurisdiction_invalid(self):
        bad = (
            "schema_version: '1.0'\n"
            "jurisdiction_profiles:\n"
            "  - jurisdiction_id: us-federal\n"
            "    name: No roots\n"
            "    country: US\n"
            "    blocked_domains: []\n"  # no official source root
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), content=bad)
            code, stdout, stderr = self.run_cmd(workspace, "validate")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        envelope = json.loads(stderr)
        self.assertEqual("JURISDICTION_INVALID", envelope["error_code"])
        self.assertFalse(envelope["details"]["network_io_executed"])

    def test_path_traversal_is_config_invalid(self):
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), jurisdictions_path="../escape.yml", content=fixture)
            code, _, stderr = self.run_cmd(workspace, "validate")
        self.assertEqual(2, code)
        self.assertEqual("CONFIG_INVALID", json.loads(stderr)["error_code"])

    def test_works_with_discovery_disabled(self):
        # Jurisdiction validation is offline and runs before the discovery gate.
        fixture = FIXTURE_YAML.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), enabled=False, content=fixture)
            code, stdout, _ = self.run_cmd(workspace, "validate")
        self.assertEqual(0, code)
        self.assertEqual(2, json.loads(stdout)["count"])


if __name__ == "__main__":
    unittest.main()
