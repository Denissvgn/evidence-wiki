"""Discovery-quality evaluation fixtures and harness (E36-T04).

A small, deterministic, network-free regression guard for discovery ranking.
Each scenario under ``tests/fixtures/discovery/eval/`` is a declarative case: raw
provider results plus the expected candidate outcomes. The harness runs the case
through the *real* discovery pipeline (legal/search via the fixture search backend,
GitHub and author-publication discovery via an injected transport) and scores three
properties the epic must not regress:

- candidate ranking (official sources outrank secondary; canonical repos outrank
  forks/mirrors; related author publications outrank out-of-scope ones), expressed
  as ``outranks`` pairs;
- trust-tier and officialness assignment per candidate;
- rejection rationale (suspicious downloads, mirrors, lower-trust duplicates of an
  official source, and out-of-scope author publications are rejected with reasoning).

Author-publication ranking quality (E35-T02) is guarded by the
``author-publications`` scenario, which proposes an ORCID-author's related work for
review and rejects an unrelated work as out of scope.
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
EVAL_DIR = REPO_ROOT / "tests" / "fixtures" / "discovery" / "eval"
JURISDICTIONS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "discovery" / "jurisdictions.yml"
READINESS_DOC = REPO_ROOT / "workspace-template" / "docs" / "production-readiness-checklist.md"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discovery_quality_eval_under_test", "discover_sources.py")


def load_scenarios() -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(EVAL_DIR.glob("*.json"))]


def candidate_key(scenario: dict, candidate: dict) -> str:
    if scenario["kind"] == "github":
        return candidate["github"]["full_name"]
    if scenario["kind"] == "authors":
        return candidate["openalex"]["work_id"]
    if scenario["kind"] == "companions":
        # Companion candidates are heterogeneous (repos, datasets, pages); the URL
        # is the one stable, human-readable identity shared by every origin.
        return candidate["url"]
    return candidate["search"]["host"]


class DiscoveryQualityEvalHarness(unittest.TestCase):
    """Runs each declarative scenario through the real discovery pipeline."""

    def run_cli(self, argv: list[str]) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        self.assertEqual(0, int(code or 0), stderr.getvalue())
        return json.loads(stdout.getvalue())

    def run_scenario(self, scenario: dict, tmpdir: Path) -> list[dict]:
        if scenario["kind"] in ("legal", "search"):
            return self._run_search_like(scenario, tmpdir)
        if scenario["kind"] == "github":
            return self._run_github(scenario, tmpdir)
        if scenario["kind"] == "authors":
            return self._run_authors(scenario, tmpdir)
        if scenario["kind"] == "companions":
            return self._run_companions(scenario, tmpdir)
        raise AssertionError(f"unknown scenario kind: {scenario['kind']}")

    def _write_workspace(self, tmpdir: Path, lines: list[str]) -> Path:
        workspace = tmpdir / "ws"
        (workspace / "sources" / "discovery" / "fixtures").mkdir(parents=True, exist_ok=True)
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return workspace

    def _run_search_like(self, scenario: dict, tmpdir: Path) -> list[dict]:
        workspace = self._write_workspace(
            tmpdir,
            [
                "project:",
                "  name: discovery-eval",
                "sources:",
                "  manifest_path: sources/manifest.jsonl",
                "integrations:",
                "  discovery:",
                "    enabled: true",
                "    providers: [search]",
                "    search:",
                "      provider: fixture",
                "      fixture_path: sources/discovery/fixtures/results.jsonl",
            ],
        )
        results = "".join(json.dumps(r) + "\n" for r in scenario["results"])
        (workspace / "sources" / "discovery" / "fixtures" / "results.jsonl").write_text(results, encoding="utf-8")

        argv = ["--project-root", str(workspace), "--format", "json", scenario["kind"]]
        if scenario["kind"] == "legal":
            (workspace / "sources" / "jurisdictions.yml").write_text(
                JURISDICTIONS_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
            )
            argv += ["--jurisdiction", scenario["jurisdiction"], "--topic", scenario["topic"]]
        else:
            argv += ["--query", scenario["query"]]
            if scenario.get("jurisdiction"):
                argv += ["--jurisdiction", scenario["jurisdiction"]]
            if scenario.get("intent"):
                argv += ["--intent", scenario["intent"]]
        argv.append("--execute")
        report = self.run_cli(argv)
        self.assertFalse(report["network_io_executed"])  # fixture backend, no network
        return report["candidates"]

    def _run_github(self, scenario: dict, tmpdir: Path) -> list[dict]:
        workspace = self._write_workspace(
            tmpdir,
            [
                "project:",
                "  name: discovery-eval",
                "integrations:",
                "  discovery:",
                "    enabled: true",
                "    providers: [github]",
            ],
        )
        payload = json.dumps({"total_count": len(scenario["items"]), "items": scenario["items"]}).encode("utf-8")
        saved = (DISCOVER.GITHUB_TRANSPORT, DISCOVER.GITHUB_CLOCK, DISCOVER.GITHUB_SLEEP, DISCOVER.GITHUB_LAST_REQUEST_AT)
        DISCOVER.GITHUB_TRANSPORT = lambda url, timeout, headers: payload
        DISCOVER.GITHUB_CLOCK = lambda: 0.0
        DISCOVER.GITHUB_SLEEP = lambda _seconds: None
        DISCOVER.GITHUB_LAST_REQUEST_AT = None
        try:
            report = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "github",
                 "--query", scenario["query"], "--max-results", "10"]
            )
        finally:
            (DISCOVER.GITHUB_TRANSPORT, DISCOVER.GITHUB_CLOCK, DISCOVER.GITHUB_SLEEP, DISCOVER.GITHUB_LAST_REQUEST_AT) = saved
        return report["candidates"]

    def _run_authors(self, scenario: dict, tmpdir: Path) -> list[dict]:
        workspace = self._write_workspace(
            tmpdir,
            [
                "project:",
                "  name: discovery-eval",
                "sources:",
                "  manifest_path: sources/manifest.jsonl",
                "  normalized_dir: sources/normalized",
                "integrations:",
                "  discovery:",
                "    enabled: true",
                "    providers: [openalex]",
            ],
        )
        paper = scenario["paper"]
        authorship = [
            {
                "author": {
                    "display_name": name,
                    "orcid": paper.get("author_orcid"),
                }
            }
            for name in paper["authors"]
        ]
        manifest_record = {
            "id": paper["source_id"],
            "kind": "paper",
            "status": "normalized",
            "metadata": {"authorships": authorship, "doi": paper.get("doi")},
        }
        (workspace / "sources" / "manifest.jsonl").write_text(
            json.dumps(manifest_record) + "\n", encoding="utf-8"
        )
        author_lines = "\n".join(f"  - {name}" for name in paper["authors"])
        normalized_body = (
            "---\n"
            "type: normalized_source\n"
            f"source_id: {paper['source_id']}\n"
            f"title: {paper['title']}\n"
            f"doi: {paper.get('doi') or ''}\n"
            "authors:\n"
            f"{author_lines}\n"
            "confidence: high\n"
            "---\n\n# body\n"
        )
        safe_id = paper["source_id"].replace(":", "--")
        normalized_path = workspace / "sources" / "normalized" / f"{safe_id}.md"
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.write_text(normalized_body, encoding="utf-8")

        payload = json.dumps({"results": scenario["works"]}).encode("utf-8")
        saved = (
            DISCOVER.OPENALEX_TRANSPORT, DISCOVER.OPENALEX_CLOCK,
            DISCOVER.OPENALEX_SLEEP, DISCOVER.OPENALEX_LAST_REQUEST_AT,
        )
        DISCOVER.OPENALEX_TRANSPORT = lambda url, timeout, headers: payload
        DISCOVER.OPENALEX_CLOCK = lambda: 0.0
        DISCOVER.OPENALEX_SLEEP = lambda _seconds: None
        DISCOVER.OPENALEX_LAST_REQUEST_AT = None
        try:
            report = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "authors",
                 "--source-id", paper["source_id"], "--discover-publications",
                 "--max-results", "10"]
            )
        finally:
            (
                DISCOVER.OPENALEX_TRANSPORT, DISCOVER.OPENALEX_CLOCK,
                DISCOVER.OPENALEX_SLEEP, DISCOVER.OPENALEX_LAST_REQUEST_AT,
            ) = saved
        self.assertTrue(report["network_io_executed"])  # OpenAlex transport, no real network
        return report["candidates"]

    def _run_companions(self, scenario: dict, tmpdir: Path) -> list[dict]:
        workspace = self._write_workspace(
            tmpdir,
            [
                "project:",
                "  name: discovery-eval",
                "sources:",
                "  manifest_path: sources/manifest.jsonl",
                "  normalized_dir: sources/normalized",
                "integrations:",
                "  discovery:",
                "    enabled: true",
                "    providers: [github, search]",
                "    search:",
                "      provider: fixture",
                "      fixture_path: sources/discovery/fixtures/results.jsonl",
            ],
        )
        paper = scenario["paper"]
        results = "".join(json.dumps(r) + "\n" for r in scenario["results"])
        (workspace / "sources" / "discovery" / "fixtures" / "results.jsonl").write_text(results, encoding="utf-8")

        metadata: dict = {}
        if paper.get("landing_page_url"):
            metadata["primary_location"] = {"landing_page_url": paper["landing_page_url"]}
        manifest_record = {"id": paper["source_id"], "kind": "paper", "status": "normalized", "metadata": metadata}
        (workspace / "sources" / "manifest.jsonl").write_text(
            json.dumps(manifest_record) + "\n", encoding="utf-8"
        )

        link_lines = "".join(f"  - {url}\n" for url in paper.get("links", []))
        author_lines = "".join(f"  - {name}\n" for name in paper.get("authors", []))
        normalized_body = (
            "---\n"
            "type: normalized_source\n"
            f"source_id: {paper['source_id']}\n"
            f"title: {json.dumps(paper['title'])}\n"
            f"arxiv_id: {json.dumps(paper.get('arxiv_id') or '')}\n"
            "authors:\n"
            f"{author_lines}"
            "links:\n"
            f"{link_lines}"
            "confidence: high\n"
            "---\n\n"
            f"{paper.get('body', '')}\n"
        )
        safe_id = paper["source_id"].replace(":", "--")
        normalized_path = workspace / "sources" / "normalized" / f"{safe_id}.md"
        normalized_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_path.write_text(normalized_body, encoding="utf-8")

        payload = json.dumps({"total_count": len(scenario["items"]), "items": scenario["items"]}).encode("utf-8")
        saved = (DISCOVER.GITHUB_TRANSPORT, DISCOVER.GITHUB_CLOCK, DISCOVER.GITHUB_SLEEP, DISCOVER.GITHUB_LAST_REQUEST_AT)
        DISCOVER.GITHUB_TRANSPORT = lambda url, timeout, headers: payload
        DISCOVER.GITHUB_CLOCK = lambda: 0.0
        DISCOVER.GITHUB_SLEEP = lambda _seconds: None
        DISCOVER.GITHUB_LAST_REQUEST_AT = None
        try:
            report = self.run_cli(
                ["--project-root", str(workspace), "--format", "json", "companions",
                 "--source-id", paper["source_id"], "--max-results", "10"]
            )
        finally:
            (DISCOVER.GITHUB_TRANSPORT, DISCOVER.GITHUB_CLOCK, DISCOVER.GITHUB_SLEEP, DISCOVER.GITHUB_LAST_REQUEST_AT) = saved
        self.assertTrue(report["network_io_executed"])  # injected GitHub transport ran
        return report["candidates"]

    def score_scenario(self, scenario: dict, produced: list[dict]) -> list[str]:
        """Return a list of human-readable failures (empty == passing)."""
        failures: list[str] = []
        by_key = {candidate_key(scenario, c): c for c in produced}
        order = [candidate_key(scenario, c) for c in produced]
        expected = scenario["expected"]

        for key, spec in expected["candidates"].items():
            candidate = by_key.get(key)
            if candidate is None:
                failures.append(f"{key}: expected candidate was not produced")
                continue
            if "trust_tier" in spec and candidate["trust_tier"] != spec["trust_tier"]:
                failures.append(f"{key}: trust_tier {candidate['trust_tier']!r} != {spec['trust_tier']!r}")
            if "official_source" in spec and candidate["official_source"] != spec["official_source"]:
                failures.append(f"{key}: official_source {candidate['official_source']!r} != {spec['official_source']!r}")
            if "recommended_action" in spec and candidate["recommended_action"] != spec["recommended_action"]:
                failures.append(
                    f"{key}: recommended_action {candidate['recommended_action']!r} != {spec['recommended_action']!r}"
                )
            for flag in spec.get("risk_flags_include", []):
                if flag not in candidate["reasoning"]["risk_flags"]:
                    failures.append(f"{key}: missing risk flag {flag!r} (have {candidate['reasoning']['risk_flags']})")
            for gate_name, gate_spec in spec.get("quality_gates", {}).items():
                gate = candidate.get("quality_gates", {}).get(gate_name)
                if gate is None:
                    failures.append(f"{key}: missing quality gate {gate_name!r}")
                    continue
                for field, expected_value in gate_spec.items():
                    if gate.get(field) != expected_value:
                        failures.append(
                            f"{key}: quality gate {gate_name}.{field} {gate.get(field)!r} != {expected_value!r}"
                        )
            include = spec.get("rationale_includes")
            if include and include.lower() not in candidate["rationale"].lower():
                failures.append(f"{key}: rationale missing {include!r}")

        for better, worse in expected.get("outranks", []):
            if better not in order:
                failures.append(f"outranks: {better!r} not produced")
            elif worse not in order:
                failures.append(f"outranks: {worse!r} not produced")
            elif order.index(better) >= order.index(worse):
                failures.append(f"outranks: {better!r} must rank before {worse!r} (order={order})")

        for key in expected.get("rejected", []):
            candidate = by_key.get(key)
            if candidate is None:
                failures.append(f"rejected: {key!r} not produced")
            elif candidate["recommended_action"] != "reject":
                failures.append(f"rejected: {key!r} recommended_action is {candidate['recommended_action']!r}, not 'reject'")
        return failures

    def test_scenarios_meet_expected_discovery_quality(self):
        scenarios = load_scenarios()
        self.assertEqual(
            {
                "legal-official-vs-secondary",
                "general-search-useful-and-rejected",
                "product-official-vs-reseller",
                "paper-companion-repository",
                "paper-companion-composite",
                "author-publications",
            },
            {s["id"] for s in scenarios},
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["id"]):
                with tempfile.TemporaryDirectory() as tmpdir:
                    produced = self.run_scenario(scenario, Path(tmpdir))
                failures = self.score_scenario(scenario, produced)
                self.assertEqual([], failures, f"{scenario['id']} regressions:\n" + "\n".join(failures))

    def test_every_scenario_is_self_documenting(self):
        for scenario in load_scenarios():
            with self.subTest(scenario=scenario["id"]):
                self.assertTrue(scenario.get("description", "").strip())
                self.assertTrue(scenario.get("expected_behavior", "").strip())
                self.assertIn("candidates", scenario["expected"])

    def test_official_preference_and_rejection_are_present_somewhere(self):
        # Guard the epic-level invariants: at least one scenario proves official
        # preference, and at least one proves rejection with rationale.
        scenarios = load_scenarios()
        self.assertTrue(
            any(
                any(c.get("trust_tier") == "official_primary" for c in s["expected"]["candidates"].values())
                for s in scenarios
            ),
            "no scenario asserts an official_primary source",
        )
        self.assertTrue(
            any(s["expected"].get("rejected") for s in scenarios),
            "no scenario asserts a rejected candidate",
        )

    def test_expected_ranking_behavior_is_documented(self):
        readiness = READINESS_DOC.read_text(encoding="utf-8")
        self.assertIn("Discovery Quality Evaluation", readiness)
        self.assertIn("tests/fixtures/discovery/eval", readiness)


if __name__ == "__main__":
    unittest.main()
