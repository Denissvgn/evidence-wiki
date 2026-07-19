"""Tests for legal candidate ranking by officialness and authority (E34-T03).

`discover_sources.py legal --execute` runs the official-source-first plan (E34-T02)
through the configured search backend and ranks the results with legal-specific
rules layered on the E33-T03 trust policy:

- a profile official-domain match is the major trust signal (official_primary);
- official gazette/legislature/regulator/court sources outrank aggregators;
- recognized secondary legal databases are retained as secondary_reputable but
  marked supplemental when an official source is available in the same run;
- non-official mirrors/duplicates of an official source are rejected;
- superseded/repealed/historical pages get a risk flag and are rejected as
  current evidence;
- every candidate's rationale states why it is official or why officialness is
  unknown, so the list is safe to hand to a reviewer.
"""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURE_JURISDICTIONS = REPO_ROOT / "tests" / "fixtures" / "discovery" / "jurisdictions.yml"
FIXTURE_RESULTS = REPO_ROOT / "tests" / "fixtures" / "discovery" / "legal-search-results.jsonl"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_legalrank_under_test", "discover_sources.py")

TRUST_BASE = {
    "official_primary": 0.9,
    "primary_non_official": 0.7,
    "secondary_reputable": 0.6,
    "secondary_unknown": 0.4,
    "unsafe_or_unusable": 0.05,
}


def make_candidate(
    host: str,
    title: str,
    *,
    tier: str = "secondary_unknown",
    official=None,
    action: str = "review",
    risk_flags=None,
    snippet: str = "",
    jurisdiction: str = "us-federal",
    url: str | None = None,
) -> dict:
    """Build a normalized candidate dict shaped like build_search_candidate output,
    for direct unit testing of refine_legal_candidates."""
    return {
        "url": url or f"https://{host}/doc",
        "title": title,
        "trust_tier": tier,
        "official_source": official,
        "recommended_action": action,
        "jurisdiction": jurisdiction,
        "trust_score": TRUST_BASE.get(tier, 0.5),
        "relevance_score": 0.8,
        "source_type": "official_legal" if tier == "official_primary" else "web_page",
        "rationale": f"Search candidate https://{host}/doc classified {tier}.",
        "reasoning": {
            "matched_query_terms": ["x"],
            "authority_reason": f"{host} authority reason.",
            "freshness_reason": "No freshness signal reported by the search provider.",
            "scope_reason": "scope.",
            "risk_flags": list(risk_flags or []),
        },
        "search": {"host": host, "snippet": snippet, "provider_rank": 1, "exact_phrase": False},
    }


def refine(candidates, *, official_domains=("govinfo.gov", "ecfr.gov"), jurisdiction="us-federal"):
    return DISCOVER.refine_legal_candidates(
        candidates, official_domains=list(official_domains), jurisdiction=jurisdiction
    )


class RefineLegalCandidatesUnitTests(unittest.TestCase):
    """The legal ranking rules, exercised directly on candidate dicts."""

    def test_official_outranks_blog_and_mirror(self):
        official = make_candidate(
            "govinfo.gov", "Clean Air Act statute official United States Code",
            tier="official_primary", official=True, action="fetch",
        )
        # A distinct blog (low title overlap with the official source) is kept.
        blog = make_candidate("blog.example", "Clean Air Act explained for small businesses")
        # A flagged mirror of the official statute is rejected.
        mirror = make_candidate(
            "mirror.example", "Clean Air Act statute",
            risk_flags=[DISCOVER.SEARCH_RISK_POSSIBLE_MIRROR],
        )
        ranked = refine([blog, mirror, official])
        ranked.sort(key=DISCOVER.search_candidate_sort_key)
        self.assertEqual("govinfo.gov", ranked[0]["search"]["host"])
        self.assertEqual("official_primary", ranked[0]["trust_tier"])
        # The mirror of an official source is rejected; the distinct blog is kept.
        by_host = {c["search"]["host"]: c for c in ranked}
        self.assertEqual("reject", by_host["mirror.example"]["recommended_action"])
        self.assertEqual("review", by_host["blog.example"]["recommended_action"])

    def test_secondary_db_retained_as_supplemental_when_official_present(self):
        official = make_candidate("govinfo.gov", "Clean Air Act", tier="official_primary", official=True, action="fetch")
        justia = make_candidate(
            "law.justia.com", "Clean Air Act", risk_flags=[DISCOVER.SEARCH_RISK_UNKNOWN_OFFICIALNESS]
        )
        ranked = refine([official, justia])
        db = next(c for c in ranked if c["search"]["host"] == "law.justia.com")
        # Retained (not dropped), upgraded to a reputable tier, known non-official.
        self.assertEqual("secondary_reputable", db["trust_tier"])
        self.assertIs(False, db["official_source"])
        self.assertEqual("review", db["recommended_action"])
        self.assertIn(DISCOVER.LEGAL_RISK_SECONDARY_WHEN_OFFICIAL, db["reasoning"]["risk_flags"])
        self.assertNotIn(DISCOVER.SEARCH_RISK_UNKNOWN_OFFICIALNESS, db["reasoning"]["risk_flags"])
        self.assertIn("supplemental", db["rationale"].lower())

    def test_secondary_db_not_rejected_even_when_title_matches_official(self):
        # A reputable DB sharing the official source's title is exempt from the
        # duplicate-of-official rejection (it is retained as supplemental).
        official = make_candidate("govinfo.gov", "Identical Statute Title", tier="official_primary", official=True, action="fetch")
        justia = make_candidate("law.justia.com", "Identical Statute Title")
        ranked = refine([official, justia])
        db = next(c for c in ranked if c["search"]["host"] == "law.justia.com")
        self.assertEqual("secondary_reputable", db["trust_tier"])
        self.assertEqual("review", db["recommended_action"])
        self.assertNotIn(DISCOVER.SEARCH_RISK_DUPLICATE_OF_OFFICIAL, db["reasoning"]["risk_flags"])

    def test_secondary_db_without_official_is_not_marked_supplemental(self):
        justia = make_candidate("law.justia.com", "Clean Air Act")
        ranked = refine([justia])
        db = ranked[0]
        self.assertEqual("secondary_reputable", db["trust_tier"])
        self.assertNotIn(DISCOVER.LEGAL_RISK_SECONDARY_WHEN_OFFICIAL, db["reasoning"]["risk_flags"])
        self.assertIn("no official source", db["rationale"].lower())

    def test_mirror_of_official_is_rejected_but_plain_mirror_without_official_is_not(self):
        mirror = make_candidate(
            "mirror.example", "Clean Air Act statute",
            risk_flags=[DISCOVER.SEARCH_RISK_POSSIBLE_MIRROR],
        )
        # No official in the run -> kept for review, not auto-rejected.
        kept = refine([make_candidate("blog.example", "unrelated"), mirror])
        mirror_kept = next(c for c in kept if c["search"]["host"] == "mirror.example")
        self.assertEqual("review", mirror_kept["recommended_action"])

    def test_superseded_page_gets_risk_flag_and_rejects_current_use(self):
        official = make_candidate(
            "govinfo.gov", "Old Rule - REPEALED prior version",
            tier="official_primary", official=True, action="fetch",
        )
        ranked = refine([official])
        flags = ranked[0]["reasoning"]["risk_flags"]
        self.assertIn(DISCOVER.LEGAL_RISK_SUPERSEDED, flags)
        # Official provenance does not make a superseded page current evidence.
        self.assertEqual("reject", ranked[0]["recommended_action"])

    def test_rationale_states_officialness(self):
        official = make_candidate("govinfo.gov", "Statute", tier="official_primary", official=True, action="fetch")
        unknown = make_candidate("blog.example", "a practitioner blog post")
        ranked = refine([official, unknown])
        by_host = {c["search"]["host"]: c for c in ranked}
        self.assertIn("official source", by_host["govinfo.gov"]["rationale"].lower())
        # With an official source present, a non-official source is marked
        # supplemental and its officialness uncertainty is stated.
        blog_rationale = by_host["blog.example"]["rationale"].lower()
        self.assertIn("unknown officialness", blog_rationale)
        self.assertIn("supplemental", blog_rationale)

    def test_unknown_source_rationale_without_official_present(self):
        # No official source in the run: the rationale states officialness is
        # unknown and asks for review before relying on it as authority.
        ranked = refine([make_candidate("blog.example", "a practitioner blog post")])
        rationale = ranked[0]["rationale"].lower()
        self.assertIn("officialness unknown", rationale)
        self.assertIn("review", rationale)

    def test_is_legal_secondary_db_matches_subdomain(self):
        self.assertTrue(DISCOVER.is_legal_secondary_db("law.justia.com"))
        self.assertTrue(DISCOVER.is_legal_secondary_db("supreme.justia.com"))
        self.assertFalse(DISCOVER.is_legal_secondary_db("govinfo.gov"))


class LegalExecuteRankingTests(unittest.TestCase):
    """End-to-end through `legal --execute` with a fixture search backend."""

    def write_workspace(self, root: Path, *, provider: bool = True, enabled: bool = True) -> Path:
        workspace = root / "ws"
        (workspace / "sources" / "discovery" / "fixtures").mkdir(parents=True, exist_ok=True)
        (workspace / "sources" / "jurisdictions.yml").write_text(
            FIXTURE_JURISDICTIONS.read_text(encoding="utf-8"), encoding="utf-8"
        )
        lines = [
            "project:",
            "  name: legal-rank-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
        ]
        if provider:
            (workspace / "sources" / "discovery" / "fixtures" / "results.jsonl").write_text(
                FIXTURE_RESULTS.read_text(encoding="utf-8"), encoding="utf-8"
            )
            lines += [
                "    providers: [search]",
                "    search:",
                "      provider: fixture",
                "      fixture_path: sources/discovery/fixtures/results.jsonl",
            ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return workspace

    def run_legal(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "legal", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def execute(self, workspace: Path) -> dict:
        code, stdout, stderr = self.run_legal(
            workspace, "--jurisdiction", "us-federal", "--topic", "clean air act", "--execute"
        )
        self.assertEqual(0, code, stderr)
        return json.loads(stdout)

    def test_execute_ranks_official_sources_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.execute(self.write_workspace(Path(tmpdir)))
        self.assertEqual("execute", report["mode"])
        self.assertEqual("legal", report["provider"])
        self.assertFalse(report["network_io_executed"])  # fixture backend
        self.assertEqual(8, report["count"])
        self.assertEqual(8, report["written"])

        candidates = report["candidates"]
        tiers = [c["trust_tier"] for c in candidates]
        # Every official_primary candidate appears before any non-official one.
        last_official = max(i for i, t in enumerate(tiers) if t == "official_primary")
        first_non_official = min(i for i, t in enumerate(tiers) if t != "official_primary")
        self.assertLess(last_official, first_non_official)
        # All candidates are attributed to legal discovery.
        self.assertTrue(all(c["provider"] == "legal" for c in candidates))
        self.assertTrue(all(c["discovered_by"] == DISCOVER.LEGAL_DISCOVERED_BY for c in candidates))

    def test_execute_classifies_each_source_correctly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.execute(self.write_workspace(Path(tmpdir)))
        by_host = {c["search"]["host"]: c for c in report["candidates"]}

        # Official statute/regulation/court sources -> official_primary, fetch.
        for host in ("govinfo.gov", "ecfr.gov", "supremecourt.gov"):
            self.assertEqual("official_primary", by_host[host]["trust_tier"], host)
            self.assertIs(True, by_host[host]["official_source"], host)
            self.assertEqual("fetch", by_host[host]["recommended_action"], host)
            self.assertIn("official source", by_host[host]["rationale"].lower(), host)

        # Superseded official page -> still official provenance, but rejected as current evidence.
        superseded = by_host["federalregister.gov"]
        self.assertEqual("official_primary", superseded["trust_tier"])
        self.assertIn(DISCOVER.LEGAL_RISK_SUPERSEDED, superseded["reasoning"]["risk_flags"])
        self.assertEqual("reject", superseded["recommended_action"])

        # Secondary legal databases -> reputable, supplemental, review.
        for host in ("law.justia.com", "findlaw.com"):
            db = by_host[host]
            self.assertEqual("secondary_reputable", db["trust_tier"], host)
            self.assertIs(False, db["official_source"], host)
            self.assertEqual("review", db["recommended_action"], host)
            self.assertIn(DISCOVER.LEGAL_RISK_SECONDARY_WHEN_OFFICIAL, db["reasoning"]["risk_flags"], host)

        # Generic blog -> unknown officialness, supplemental review (not rejected).
        blog = by_host["greenlawblog.example"]
        self.assertEqual("secondary_unknown", blog["trust_tier"])
        self.assertEqual("review", blog["recommended_action"])

        # Mirror of the official statute -> rejected.
        mirror = by_host["archive-mirror.example"]
        self.assertEqual("reject", mirror["recommended_action"])

    def test_every_candidate_has_nonempty_rationale(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            report = self.execute(self.write_workspace(Path(tmpdir)))
        for candidate in report["candidates"]:
            self.assertTrue(candidate["rationale"].strip())
            self.assertIn("recommended_action", candidate["rationale"])

    def test_execute_without_provider_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), provider=False)
            code, stdout, stderr = self.run_legal(
                workspace, "--jurisdiction", "us-federal", "--topic", "x", "--execute"
            )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("DISCOVERY_PROVIDER_DISABLED", json.loads(stderr)["error_code"])

    def test_plan_mode_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            code, stdout, _ = self.run_legal(workspace, "--jurisdiction", "us-federal", "--topic", "x")
            store = workspace / "sources" / "discovery" / "candidates.jsonl"
            self.assertEqual(0, code)
            self.assertEqual("plan", json.loads(stdout)["mode"])
            self.assertFalse(store.exists())


if __name__ == "__main__":
    unittest.main()
