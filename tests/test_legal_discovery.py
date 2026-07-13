"""Tests for legal discovery query planning (E34-T02).

`discover_sources.py legal --jurisdiction TEXT --topic TEXT` expands a legal topic
into an official-source-first query plan for a jurisdiction. It is profile-driven:
it loads the matched jurisdiction profile (E34-T01) and threads its official
domains and entry-point roots into each planned query, distinguishing the legal
source categories (statute, regulation, agency guidance, court opinion, official
form, gazette/legislative-history notice).

Planning is read-only: it produces an explained plan and never contacts a search
backend (`network_io_executed: false`). A missing or incomplete profile is a
warning, not an error -- the plan is still produced, just without official-domain
prioritization. Backend execution and legal candidate ranking land in E34-T03.
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
FIXTURE_YAML = REPO_ROOT / "tests" / "fixtures" / "discovery" / "jurisdictions.yml"

# The legal source categories the plan must distinguish (E34-T02).
EXPECTED_LEGAL_CATEGORIES = {
    "statute",
    "regulation",
    "agency_guidance",
    "court_opinion",
    "official_form",
    "gazette_notice",
}


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_legal_under_test", "discover_sources.py")


class LegalTestBase(unittest.TestCase):
    def write_workspace(
        self,
        root: Path,
        *,
        enabled: bool = True,
        jurisdictions_content: str | None = None,
        jurisdictions_path: str = "sources/jurisdictions.yml",
    ) -> Path:
        workspace = root / "ws"
        (workspace / "sources").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: legal-discovery-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
        ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if jurisdictions_content is not None:
            target = workspace / jurisdictions_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(jurisdictions_content, encoding="utf-8")
        return workspace

    def fixture_workspace(self, root: Path, **kwargs) -> Path:
        return self.write_workspace(
            root, jurisdictions_content=FIXTURE_YAML.read_text(encoding="utf-8"), **kwargs
        )

    def run_legal(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "legal", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def plan(self, workspace: Path, *args: str) -> dict:
        code, stdout, stderr = self.run_legal(workspace, *args)
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def store_records(self, workspace: Path) -> list[dict]:
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class LegalPlanShapeTests(LegalTestBase):
    """The plan distinguishes legal categories and is official-source-first."""

    def test_country_profile_drives_official_domains(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "us-federal", "--topic", "emissions reporting",
            )
        self.assertEqual("legal", report["command"])
        self.assertEqual("plan", report["mode"])
        self.assertTrue(report["jurisdiction_resolved"])
        self.assertEqual("us-federal", report["jurisdiction"])
        self.assertEqual("US", report["country"])
        self.assertIsNone(report["state_or_region"])
        self.assertEqual(
            ["govinfo.gov", "ecfr.gov", "federalregister.gov", "regs.gov"],
            report["official_domains"],
        )
        self.assertEqual([], report["warnings"])
        self.assertFalse(report["network_io_executed"])

    def test_plan_distinguishes_all_legal_categories(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "us-federal", "--topic", "emissions",
            )
        queries = report["planned_queries"]
        self.assertEqual(6, report["planned_query_count"])
        self.assertEqual(EXPECTED_LEGAL_CATEGORIES, {q["legal_category"] for q in queries})
        # Every query is an official-source-first legal query.
        self.assertTrue(all(q["expected_source_type"] == "official_legal" for q in queries))
        self.assertTrue(all(q["prefer_official"] is True for q in queries))
        # The topic is embedded in every query, with the category term appended.
        self.assertTrue(all(q["query"].startswith("emissions ") for q in queries))
        self.assertIn("emissions statute", [q["query"] for q in queries])

    def test_official_domain_query_construction(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "us-federal", "--topic", "wetlands",
            )
        official = report["official_domains"]
        by_category = {q["legal_category"]: q for q in report["planned_queries"]}
        # Each planned query prioritizes the profile's official domains.
        for query in report["planned_queries"]:
            self.assertEqual(official, query["domain_allowlist"])
        # Category entry-point roots come from the matching profile field.
        self.assertEqual(["https://www.congress.gov"], by_category["statute"]["profile_roots"])
        self.assertEqual(["https://www.supremecourt.gov"], by_category["court_opinion"]["profile_roots"])
        self.assertEqual(
            ["https://www.govinfo.gov/app/collection/fr"], by_category["gazette_notice"]["profile_roots"]
        )

    def test_state_profile_matching_and_blocked_domains(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "us-ca", "--topic", "rent control",
            )
        self.assertEqual("us-ca", report["jurisdiction"])
        self.assertEqual("CA", report["state_or_region"])
        self.assertEqual(
            ["leginfo.legislature.ca.gov", "oal.ca.gov", "courts.ca.gov"],
            report["official_domains"],
        )
        # blocked_domains flow into every query's domain_blocklist.
        self.assertEqual(["example.com"], report["blocked_domains"])
        self.assertTrue(all(q["domain_blocklist"] == ["example.com"] for q in report["planned_queries"]))

    def test_jurisdiction_resolves_by_display_name(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "California (State)", "--topic", "zoning",
            )
        self.assertTrue(report["jurisdiction_resolved"])
        self.assertEqual("us-ca", report["jurisdiction"])
        self.assertEqual("California (State)", report["jurisdiction_name"])

    def test_max_results_is_recorded(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "us-federal", "--topic", "x", "--max-results", "3",
            )
        self.assertEqual(3, report["max_results"])


class LegalWarningTests(LegalTestBase):
    """Missing/incomplete profiles warn but still produce a plan."""

    def test_unknown_jurisdiction_warns_and_still_plans(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            report = self.plan(
                self.fixture_workspace(Path(tmpdir)),
                "--jurisdiction", "atlantis", "--topic", "shipping",
            )
        self.assertFalse(report["jurisdiction_resolved"])
        self.assertEqual(["no_jurisdiction_profile"], [w["code"] for w in report["warnings"]])
        self.assertEqual([], report["official_domains"])
        # A generic legal plan is still produced for all categories.
        self.assertEqual(6, report["planned_query_count"])
        self.assertEqual(EXPECTED_LEGAL_CATEGORIES, {q["legal_category"] for q in report["planned_queries"]})

    def test_no_profiles_file_warns(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))  # no jurisdictions.yml
            report = self.plan(workspace, "--jurisdiction", "us-federal", "--topic", "x")
        self.assertFalse(report["jurisdiction_resolved"])
        self.assertEqual(["no_jurisdiction_profile"], [w["code"] for w in report["warnings"]])

    def test_profile_without_official_domains_warns(self):
        # A valid profile (E34-T01 requires an official root) that supplies only
        # URL roots -- no bare official_domains -- still plans, with a warning.
        content = (
            'schema_version: "1.0"\n'
            "jurisdiction_profiles:\n"
            "  - jurisdiction_id: us-tx\n"
            "    name: Texas (State)\n"
            "    country: US\n"
            "    state_or_region: TX\n"
            "    legislature_urls: [https://capitol.texas.gov]\n"
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), jurisdictions_content=content)
            report = self.plan(workspace, "--jurisdiction", "us-tx", "--topic", "water rights")
        self.assertTrue(report["jurisdiction_resolved"])
        self.assertEqual(["no_official_domains"], [w["code"] for w in report["warnings"]])
        self.assertEqual([], report["official_domains"])
        # The category root still surfaces the legislature entry point.
        by_category = {q["legal_category"]: q for q in report["planned_queries"]}
        self.assertEqual(["https://capitol.texas.gov"], by_category["statute"]["profile_roots"])


class LegalGateAndSafetyTests(LegalTestBase):
    """The discovery gate, argument validation, and read-only guarantees."""

    def test_disabled_discovery_refuses(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.fixture_workspace(Path(tmpdir), enabled=False)
            code, stdout, stderr = self.run_legal(workspace, "--jurisdiction", "us-federal", "--topic", "x")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    def test_empty_topic_is_value_invalid(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.fixture_workspace(Path(tmpdir))
            code, _, stderr = self.run_legal(workspace, "--jurisdiction", "us-federal", "--topic", "   ")
        self.assertEqual(2, code)
        self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])

    def test_malformed_profiles_file_is_jurisdiction_invalid(self):
        bad = (
            'schema_version: "1.0"\n'
            "jurisdiction_profiles:\n"
            "  - jurisdiction_id: us-federal\n"
            "    name: No roots\n"
            "    country: US\n"  # no official source root -> invalid (E34-T01)
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), jurisdictions_content=bad)
            code, _, stderr = self.run_legal(workspace, "--jurisdiction", "us-federal", "--topic", "x")
        self.assertEqual(2, code)
        self.assertEqual("JURISDICTION_INVALID", json.loads(stderr)["error_code"])

    def test_plan_is_read_only(self):
        def forbid_socket(*args, **kwargs):  # pragma: no cover - only fires on a bug
            raise AssertionError("legal planning must not open a network socket")

        original = socket.socket
        socket.socket = forbid_socket
        try:
            with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
                workspace = self.fixture_workspace(Path(tmpdir))
                report = self.plan(workspace, "--jurisdiction", "us-federal", "--topic", "x")
                stored = self.store_records(workspace)
        finally:
            socket.socket = original
        self.assertFalse(report["network_io_executed"])
        # Planning proposes; it writes nothing to the durable candidate store.
        self.assertEqual([], stored)


class LegalHelperTests(unittest.TestCase):
    """Unit-level coverage of the resolution and plan-building helpers."""

    def setUp(self):
        self.profile = DISCOVER.validate_jurisdiction_profile(
            {
                "jurisdiction_id": "us-federal",
                "name": "United States (Federal)",
                "country": "US",
                "official_domains": ["govinfo.gov"],
                "court_urls": ["https://www.supremecourt.gov"],
                "blocked_domains": ["bad.example"],
            },
            set(),
        )

    def test_resolve_by_id_then_name(self):
        profiles = [self.profile]
        self.assertIs(self.profile, DISCOVER.find_jurisdiction_by_id_or_name(profiles, "US-Federal"))
        self.assertIs(
            self.profile, DISCOVER.find_jurisdiction_by_id_or_name(profiles, "united states (federal)")
        )
        self.assertIsNone(DISCOVER.find_jurisdiction_by_id_or_name(profiles, "nope"))

    def test_build_plan_threads_profile_roots(self):
        plan = DISCOVER.build_legal_query_plan(
            "clean air",
            profile=self.profile,
            jurisdiction_label="us-federal",
            official_domains=["govinfo.gov"],
            blocked_domains=["bad.example"],
        )
        by_category = {q["legal_category"]: q for q in plan}
        self.assertEqual(EXPECTED_LEGAL_CATEGORIES, set(by_category))
        self.assertEqual(["https://www.supremecourt.gov"], by_category["court_opinion"]["profile_roots"])
        # A category with no matching profile field simply carries no roots.
        self.assertEqual([], by_category["statute"]["profile_roots"])
        self.assertTrue(all(q["domain_allowlist"] == ["govinfo.gov"] for q in plan))
        self.assertTrue(all(q["domain_blocklist"] == ["bad.example"] for q in plan))

    def test_build_plan_without_profile_has_empty_roots(self):
        plan = DISCOVER.build_legal_query_plan(
            "clean air",
            profile=None,
            jurisdiction_label="atlantis",
            official_domains=[],
            blocked_domains=[],
        )
        self.assertTrue(all(q["profile_roots"] == [] for q in plan))
        self.assertTrue(all(q["domain_allowlist"] == [] for q in plan))
        self.assertTrue(all(q["jurisdiction"] == "atlantis" for q in plan))


if __name__ == "__main__":
    unittest.main()
