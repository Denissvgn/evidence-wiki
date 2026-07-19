import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    loader_path = SCRIPTS / "_workspace_module_loader.py"
    if path != loader_path and loader_path.is_file():
        loader_name = f"{name}_isolated_loader"
        loader_spec = importlib.util.spec_from_file_location(loader_name, loader_path)
        if loader_spec is None or loader_spec.loader is None:
            raise RuntimeError(f"Cannot load workspace module loader from {loader_path}")
        loader_module = importlib.util.module_from_spec(loader_spec)
        sys.modules[loader_name] = loader_module
        try:
            loader_spec.loader.exec_module(loader_module)
        finally:
            sys.modules.pop(loader_name, None)
        return loader_module.load_workspace_module(SCRIPTS, path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("autonomous_fixture_init", "init_research_workspace.py")
INVENTORY = load_script_module("autonomous_fixture_inventory", "source_inventory.py")
NORMALIZE = load_script_module("autonomous_fixture_normalize", "normalize_sources.py")
COVERAGE = load_script_module("autonomous_fixture_coverage", "coverage_manifest.py")
EXPORT = load_script_module("autonomous_fixture_export", "export_answers.py")
READINESS = load_script_module("autonomous_fixture_readiness", "publication_readiness.py")
WORKSPACE_STATUS = load_script_module("autonomous_fixture_status", "workspace_status.py")


class AutonomousWorkflowFixtureTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        target = root / "autonomous-fixture"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text(encoding="utf-8"))
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = [
            {
                "id": "mixed-domain-publication",
                "question": "Can the mixed-domain fixture be published?",
                "priority": "high",
            }
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = INIT.main(["--profile", str(profile_path)])
        self.assertEqual(0, int(code or 0))

        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["raw"]["source_roots"] = ["raw/papers", "raw/links", "raw/web"]
        config.setdefault("sources", {})
        config["sources"]["coverage_dir"] = "sources/coverage"
        config.setdefault("integrations", {})
        config["integrations"]["discovery"] = {
            "enabled": True,
            "providers": ["openalex"],
            "jurisdictions_path": "sources/jurisdictions.yml",
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return target

    def run_json_script(self, module: Any, args: list[str]) -> tuple[int, dict[str, Any], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(args)
        payload_text = stdout.getvalue().strip() or stderr.getvalue().strip()
        payload = json.loads(payload_text) if payload_text else {}
        return int(code or 0), payload, stderr.getvalue()

    def write_raw_file(self, workspace: Path, relative: str, content: str, provenance: dict[str, Any]) -> None:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        checksum = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        sidecar = dict(provenance)
        sidecar["checksum"] = checksum
        (workspace / f"{relative}.provenance.yml").write_text(
            yaml.safe_dump(sidecar, sort_keys=False),
            encoding="utf-8",
        )

    def seed_raw_sources(self, workspace: Path) -> None:
        self.write_raw_file(
            workspace,
            "raw/papers/academic-method.html",
            (
                "<html><head><title>Academic Method Fixture</title></head>"
                "<body>Peer-reviewed method evidence.</body></html>\n"
            ),
            {
                "origin_url": "https://arxiv.org/abs/2601.12345v1",
                "retrieved_at": "2026-07-03T00:00:00Z",
                "retrieved_by": "fetch-agent/fixture",
                "academic_provider": "arxiv",
                "academic_source_type": "paper",
                "publication_year": 2026,
                "doi": "10.5555/autonomous-fixture-method",
                "arxiv_id": "2601.12345v1",
                "license": "CC-BY-4.0",
                "terms_url": "https://arxiv.org/help/license",
                "terms_note": "Synthetic fixture mirrors an indexed academic source.",
                "notes": "Academic evidence selected for the offline mixed-domain fixture.",
                "request_id": "req-academic-method",
                "candidate_id": "cand-academic-method",
            },
        )
        self.write_raw_file(
            workspace,
            "raw/web/official-guidance.html",
            (
                "<html><head><title>Official Current Figure</title></head>"
                "<body>Official public guidance for the current figure.</body></html>\n"
            ),
            {
                "origin_url": "https://official.example/current-figure",
                "retrieved_at": "2026-07-03T00:00:00Z",
                "retrieved_by": "fetch-agent/fixture",
                "license": "CC0-1.0",
                "validity_period": "2026-01-01/2099-12-31",
                "terms_note": "Official public-domain fixture source.",
                "notes": "Official source selected over lower-trust secondary commentary.",
                "request_id": "req-official-current",
                "candidate_id": "cand-official-current",
            },
        )
        self.write_raw_file(
            workspace,
            "raw/web/vendor-product.html",
            (
                "<html><head><title>Official Product Spec</title></head>"
                "<body>Vendor-controlled product specification.</body></html>\n"
            ),
            {
                "origin_url": "https://docs.vendor.example/product/spec",
                "retrieved_at": "2026-07-03T00:00:00Z",
                "retrieved_by": "fetch-agent/fixture",
                "license": "CC-BY-4.0",
                "terms_url": "https://vendor.example/terms",
                "terms_note": "Official vendor terms reviewed for the fixture.",
                "date_not_available": "Official vendor spec page exposes no publication date.",
                "notes": "Official vendor product specification selected for publication.",
                "request_id": "req-vendor-product",
                "candidate_id": "cand-vendor-product",
            },
        )
        self.write_raw_file(
            workspace,
            "raw/web/secondary-commentary.html",
            (
                "<html><head><title>Secondary Commentary</title></head>"
                "<body>Lower-trust commentary that must not be used as evidence.</body></html>\n"
            ),
            {
                "origin_url": "https://blog.example/current-figure",
                "retrieved_at": "2026-07-03T00:00:00Z",
                "retrieved_by": "fetch-agent/fixture",
                "license": "CC-BY-4.0",
                "terms_note": "Rejected lower-trust commentary fixture.",
                "notes": "Rejected because an official source is available.",
            },
        )
        links = workspace / "raw" / "links" / "github-repository.txt"
        links.parent.mkdir(parents=True, exist_ok=True)
        links.write_text("https://github.com/acme/autonomous-fixture\n", encoding="utf-8")

    def read_manifest(self, workspace: Path) -> list[dict[str, Any]]:
        manifest = workspace / "sources" / "manifest.jsonl"
        return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    def source_by_raw_path(self, records: list[dict[str, Any]], raw_path: str) -> str:
        matches = [record["id"] for record in records if raw_path in record.get("raw_paths", [])]
        self.assertEqual(1, len(matches), f"expected one manifest record for {raw_path}")
        return matches[0]

    def source_by_url(self, records: list[dict[str, Any]], url: str) -> str:
        matches = [record["id"] for record in records if record.get("url") == url]
        self.assertEqual(1, len(matches), f"expected one manifest record for {url}")
        return matches[0]

    def write_candidates(
        self,
        workspace: Path,
        *,
        academic_id: str,
        official_id: str,
        vendor_id: str,
        github_id: str,
        secondary_id: str,
    ) -> None:
        candidates = [
            {
                "schema_version": "1.0",
                "candidate_id": "cand-academic-method",
                "provider": "search",
                "url": "https://arxiv.org/abs/2601.12345v1",
                "title": "Academic Method Fixture",
                "source_type": "paper",
                "trust_tier": "primary",
                "official_source": False,
                "recommended_action": "fetch",
                "status": "fetched",
                "selected_for_request_id": "req-academic-method",
                "fetched_source_id": academic_id,
                "evidence_path": "academic_method_existence",
            },
            {
                "schema_version": "1.0",
                "candidate_id": "cand-official-current",
                "provider": "search",
                "url": "https://official.example/current-figure",
                "title": "Official Current Figure",
                "source_type": "official_web",
                "trust_tier": "official_primary",
                "official_source": True,
                "recommended_action": "fetch",
                "status": "fetched",
                "selected_for_request_id": "req-official-current",
                "fetched_source_id": official_id,
                "evidence_path": "legal_current_figure",
            },
            {
                "schema_version": "1.0",
                "candidate_id": "cand-vendor-product",
                "provider": "search",
                "url": "https://docs.vendor.example/product/spec",
                "title": "Official Product Spec",
                "source_type": "web_page",
                "trust_tier": "official_primary",
                "official_source": True,
                "recommended_action": "fetch",
                "status": "fetched",
                "selected_for_request_id": "req-vendor-product",
                "fetched_source_id": vendor_id,
                "evidence_path": "vendor_product_spec",
            },
            {
                "schema_version": "1.0",
                "candidate_id": "cand-github-metadata",
                "provider": "github",
                "url": "https://github.com/acme/autonomous-fixture",
                "title": "acme/autonomous-fixture",
                "source_type": "repository_metadata",
                "trust_tier": "primary",
                "official_source": False,
                "recommended_action": "fetch",
                "status": "fetched",
                "selected_for_request_id": "req-github-metadata",
                "fetched_source_id": github_id,
                "evidence_path": "github_implementation",
            },
            {
                "schema_version": "1.0",
                "candidate_id": "cand-secondary-commentary",
                "provider": "search",
                "url": "https://blog.example/current-figure",
                "title": "Secondary Commentary",
                "source_type": "web_page",
                "trust_tier": "secondary",
                "official_source": False,
                "recommended_action": "reject",
                "status": "rejected",
                "selected_for_request_id": None,
                "fetched_source_id": secondary_id,
                "evidence_path": "legal_current_figure",
                "rejection_reason": "lower-trust duplicate of official source",
            },
        ]
        path = workspace / "sources" / "discovery" / "candidates.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in candidates), encoding="utf-8")

    def write_jurisdiction_profile(self, workspace: Path) -> None:
        path = workspace / "sources" / "jurisdictions.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                {
                    "jurisdiction_profiles": [
                        {
                            "jurisdiction_id": "fixture-official",
                            "name": "Fixture Official Domain",
                            "country": "xx",
                            "official_domains": ["official.example"],
                            "blocked_domains": ["blog.example"],
                            "legislature_urls": [],
                            "regulator_urls": [],
                            "court_urls": [],
                            "gazette_urls": [],
                        }
                    ]
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def write_answer_and_coverage(
        self,
        workspace: Path,
        *,
        academic_id: str,
        official_id: str,
        vendor_id: str,
        github_id: str,
    ) -> None:
        source_ids = [academic_id, github_id, official_id, vendor_id]
        answer = workspace / "wiki" / "synthesis" / "mixed-domain-publication.md"
        answer.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-07-03\n"
            "updated: 2026-07-03\n"
            "source_ids:\n"
            + "".join(f"  - {source_id}\n" for source_id in source_ids)
            + "summary: The mixed-domain fixture is grounded in accepted academic, GitHub, official, and vendor evidence.\n"
            "---\n\n"
            "# Mixed-Domain Publication\n\n"
            "The fixture answer is grounded in selected evidence from every required path.\n",
            encoding="utf-8",
        )
        for source_id in source_ids:
            note = workspace / "wiki" / "sources" / f"{source_id.replace(':', '-')}-source.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text(
                "---\n"
                "type: source\n"
                "created: 2026-07-03\n"
                "updated: 2026-07-03\n"
                "source_ids:\n"
                f"  - {source_id}\n"
                "---\n\n"
                f"# Source {source_id}\n\nFixture source note for the autonomous workflow harness.\n",
                encoding="utf-8",
            )

        question = workspace / "wiki" / "questions" / "mixed-domain-publication.md"
        text = question.read_text(encoding="utf-8")
        text = text.replace("status: open", "status: answered", 1)
        text = text.replace(
            "source_ids: []",
            "source_ids:\n"
            + "".join(f"  - {source_id}\n" for source_id in source_ids)
            + "answer_page: ../synthesis/mixed-domain-publication.md\n"
            "coverage_required: true\n"
            "coverage_manifest: sources/coverage/mixed-domain-publication.yml\n"
            "answered_by: fixture-answer-agent\n"
            "grounding:\n"
            "  - claim: Academic method evidence is present.\n"
            f"    source_id: {academic_id}\n"
            "    quote: Peer-reviewed method evidence.\n"
            "    location_hint: Academic method fixture\n"
            "  - claim: GitHub repository metadata is present.\n"
            f"    source_id: {github_id}\n"
            "    quote: https://github.com/acme/autonomous-fixture\n"
            "    location_hint: Links\n"
            "  - claim: Official current source evidence is present.\n"
            f"    source_id: {official_id}\n"
            "    quote: Official public guidance for the current figure.\n"
            "    location_hint: Official current figure fixture\n"
            "  - claim: Vendor product specification evidence is present.\n"
            f"    source_id: {vendor_id}\n"
            "    quote: Vendor-controlled product specification.\n"
            "    location_hint: Official product spec fixture\n"
            "confidence: high\n"
            "evidence_strength: corroborated",
            1,
        )
        question.write_text(text, encoding="utf-8")

        coverage = {
            "schema_version": "1.0",
            "question_slug": "mixed-domain-publication",
            "created_at": "2026-07-03T00:00:00Z",
            "updated_at": "2026-07-03T00:00:00Z",
            "coverage_profile": "offline-multi-path-publication",
            "coverage_verdict": "pending",
            "required_facets": [
                {
                    "facet_id": "academic-method",
                    "description": "Academic source has local indexed citation identity.",
                    "required": True,
                    "evidence_path": "academic_method_existence",
                    "source_policy": "academic_indexed",
                    "freshness_policy": "publication_identity",
                    "identity_policy": "citation_id_resolves",
                    "min_sources": 1,
                    "accepted_source_ids": [academic_id],
                    "blocking_request_ids": [],
                    "facet_verdict": "pending",
                },
                {
                    "facet_id": "github-metadata",
                    "description": "GitHub metadata source identifies the repository candidate.",
                    "required": True,
                    "evidence_path": "github_implementation",
                    "source_policy": "primary_or_official",
                    "freshness_policy": "no_staleness_check",
                    "identity_policy": "none",
                    "min_sources": 1,
                    "accepted_source_ids": [github_id],
                    "blocking_request_ids": [],
                    "facet_verdict": "pending",
                    "accepted_artifact_kinds": ["repository_metadata"],
                },
                {
                    "facet_id": "official-current",
                    "description": "Official current source outranks secondary commentary.",
                    "required": True,
                    "evidence_path": "legal_current_figure",
                    "source_policy": "official_primary",
                    "freshness_policy": "current_legal_figure",
                    "identity_policy": "official_domain_match",
                    "min_sources": 1,
                    "accepted_source_ids": [official_id],
                    "blocking_request_ids": [],
                    "facet_verdict": "pending",
                },
                {
                    "facet_id": "vendor-product",
                    "description": "Official vendor product specification is selected and current enough.",
                    "required": True,
                    "evidence_path": "vendor_product_spec",
                    "source_policy": "official_vendor",
                    "freshness_policy": "current_product_spec",
                    "identity_policy": "origin_url_matches_candidate",
                    "min_sources": 1,
                    "accepted_source_ids": [vendor_id],
                    "blocking_request_ids": [],
                    "facet_verdict": "pending",
                },
            ],
            "optional_facets": [],
        }
        coverage_path = workspace / "sources" / "coverage" / "mixed-domain-publication.yml"
        coverage_path.parent.mkdir(parents=True, exist_ok=True)
        coverage_path.write_text(yaml.safe_dump(coverage, sort_keys=False), encoding="utf-8")

    def test_offline_multi_path_fixture_reaches_publication_ship(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            self.seed_raw_sources(workspace)

            code, inventory_report, _ = self.run_json_script(
                INVENTORY, ["--project-root", str(workspace), "--report", "--format", "json"]
            )
            self.assertEqual(0, code)
            self.assertEqual("ready_for_normalization", inventory_report["readiness"])

            code, normalize_report, _ = self.run_json_script(
                NORMALIZE, ["--project-root", str(workspace), "--all", "--format", "json"]
            )
            self.assertEqual(0, code)
            self.assertGreaterEqual(normalize_report["summary"]["created"], 5)

            records = self.read_manifest(workspace)
            academic_id = self.source_by_raw_path(records, "raw/papers/academic-method.html")
            official_id = self.source_by_raw_path(records, "raw/web/official-guidance.html")
            vendor_id = self.source_by_raw_path(records, "raw/web/vendor-product.html")
            secondary_id = self.source_by_raw_path(records, "raw/web/secondary-commentary.html")
            github_id = self.source_by_url(records, "https://github.com/acme/autonomous-fixture")

            self.write_jurisdiction_profile(workspace)
            self.write_candidates(
                workspace,
                academic_id=academic_id,
                official_id=official_id,
                vendor_id=vendor_id,
                github_id=github_id,
                secondary_id=secondary_id,
            )
            self.write_answer_and_coverage(
                workspace,
                academic_id=academic_id,
                official_id=official_id,
                vendor_id=vendor_id,
                github_id=github_id,
            )

            code, coverage_report, _ = self.run_json_script(
                COVERAGE,
                [
                    "--project-root",
                    str(workspace),
                    "evaluate",
                    "--slug",
                    "mixed-domain-publication",
                    "--format",
                    "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual("pass", coverage_report["coverage_verdict"])

            code, status, _ = self.run_json_script(
                WORKSPACE_STATUS, ["--project-root", str(workspace), "--format", "json"]
            )
            self.assertEqual(0, code)
            self.assertEqual("complete", status["readiness"]["verdict"])
            self.assertEqual(1, status["candidates"]["rejections"]["with_reason"])

            code, export, _ = self.run_json_script(
                EXPORT, ["--project-root", str(workspace), "--format", "json"]
            )
            self.assertEqual(0, code)
            question = export["questions"][0]
            self.assertEqual("pass", question["coverage_status"])
            self.assertEqual(4, len(question["candidate_trace"]))
            self.assertTrue(question["policy_results"])
            self.assertTrue(question["currentness"])

            code, readiness, _ = self.run_json_script(
                READINESS, ["--project-root", str(workspace), "--format", "json"]
            )

        self.assertEqual(0, code)
        self.assertEqual("ship", readiness["verdict"])
        self.assertFalse(readiness["network_io_executed"])
        self.assertEqual([], readiness["reasons"]["coverage"])
        self.assertEqual([], readiness["reasons"]["safety"])
