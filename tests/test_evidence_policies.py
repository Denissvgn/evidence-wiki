import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests._pack_policy_rule_fixture import (  # noqa: E402
    CANDIDATE_SKU,
    FRESHNESS_DEFINITION,
    FRESHNESS_POLICY,
    FRESHNESS_RULE,
    IDENTITY_DEFINITION,
    IDENTITY_POLICY,
    IDENTITY_RULE,
    PACK_NAME,
    PROVIDER_ID,
    SOURCE_POLICY,
    SOURCE_RULE,
    declare_pack,
    deliver_quote,
)

SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
POLICY_HELPER_PATH = SCRIPTS / "_evidence_policies.py"


def load_policy_helper():
    if not POLICY_HELPER_PATH.is_file():
        raise AssertionError(f"missing workspace script: {POLICY_HELPER_PATH}")
    spec = importlib.util.spec_from_file_location("evidence_policies_under_test", POLICY_HELPER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from {POLICY_HELPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_frontmatter(path: Path, frontmatter: dict[str, Any], body: str = "# Source\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body,
        encoding="utf-8",
    )


class EvidencePolicyHelperTests(unittest.TestCase):
    def write_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        (workspace / "raw" / "web").mkdir(parents=True)
        (workspace / "raw" / "papers").mkdir(parents=True)
        (workspace / "raw" / "links").mkdir(parents=True)
        (workspace / "raw" / "code").mkdir(parents=True)
        (workspace / "sources" / "normalized").mkdir(parents=True)
        (workspace / "sources" / "discovery").mkdir(parents=True)
        (workspace / "sources" / "coverage").mkdir(parents=True)

        (workspace / "research.yml").write_text(
            "\n".join(
                [
                    "project:",
                    "  name: evidence-policy-fixture",
                    "sources:",
                    "  manifest_path: sources/manifest.jsonl",
                    "  normalized_dir: sources/normalized",
                    "  coverage_dir: sources/coverage",
                    "integrations:",
                    "  discovery:",
                    "    jurisdictions_path: sources/jurisdictions.yml",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        manifest_records = [
            {
                "id": "web:seg-social-cuota",
                "kind": "html",
                "raw_paths": ["raw/web/seg-social-cuota.html"],
                "status": "normalized",
                "detected_at": "2026-06-29T00:00:00Z",
            },
            {
                "id": "paper:turboquant",
                "kind": "paper",
                "raw_paths": ["raw/papers/turboquant.md"],
                "status": "normalized",
                "detected_at": "2026-06-29T00:00:00Z",
            },
            {
                "id": "github:acme-tool-v1.2.3",
                "kind": "code_archive",
                "raw_paths": ["raw/code/github-acme-tool-v1.2.3.tar.gz"],
                "status": "normalized",
                "detected_at": "2026-06-29T00:00:00Z",
                "provenance": {
                    "origin_url": "https://github.com/acme/tool",
                    "downloaded_archive_url": "https://api.github.com/repos/acme/tool/tarball/v1.2.3",
                    "repository_owner": "acme",
                    "repository_name": "tool",
                    "repository_full_name": "acme/tool",
                    "repository_artifact_kind": "source_archive",
                    "repository_ref": "v1.2.3",
                    "commit_sha": "a" * 40,
                    "license": "MIT",
                    "retrieved_at": "2026-06-29T00:00:00Z",
                    "checksum": "sha256:" + "1" * 64,
                    "checksum_verified": True,
                },
            },
            {
                "id": "web:vendor-official-product-spec",
                "kind": "html",
                "raw_paths": ["raw/web/vendor-product.html"],
                "status": "normalized",
                "detected_at": "2026-06-29T00:00:00Z",
            },
            {
                "id": "web:bare-secondary",
                "kind": "html",
                "raw_paths": ["raw/web/bare-secondary.html"],
                "status": "normalized",
                "detected_at": "2026-06-29T00:00:00Z",
            },
        ]
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in manifest_records),
            encoding="utf-8",
        )

        raw_files = {
            "raw/web/seg-social-cuota.html": "<html>Official fee amount.</html>\n",
            "raw/papers/turboquant.md": "TurboQuant paper fixture.\n",
            "raw/code/github-acme-tool-v1.2.3.tar.gz": "fake archive bytes\n",
            "raw/web/vendor-product.html": "<html>Vendor product specification.</html>\n",
            "raw/web/bare-secondary.html": "<html>Commentary page.</html>\n",
        }
        for relative, content in raw_files.items():
            (workspace / relative).write_text(content, encoding="utf-8")

        (workspace / "raw" / "web" / "seg-social-cuota.html.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://seg-social.es/wps/cuota-reducida",
                    "retrieved_at": "2026-06-29T00:00:00Z",
                    "retrieved_by": "fixture",
                    "license": "CC-BY-4.0",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (workspace / "raw" / "web" / "vendor-product.html.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://docs.vendor.example/product/spec",
                    "retrieved_at": "2026-06-29T00:00:00Z",
                    "retrieved_by": "fixture",
                    "request_id": "req-vendor-spec",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        write_frontmatter(
            workspace / "sources" / "normalized" / "paper--turboquant.md",
            {
                "type": "normalized_source",
                "source_id": "paper:turboquant",
                "source_kind": "paper",
                "status": "content_extracted",
                "raw_paths": ["raw/papers/turboquant.md"],
                "manifest_path": "sources/manifest.jsonl",
                "title": "TurboQuant",
                "authors": ["Ada Lovelace", "Grace Hopper"],
                "publication_year": 2026,
                "doi": "10.5555/turboquant",
                "arxiv_id": "2601.00001v2",
                "openalex_id": "W123456789",
                "url": "https://arxiv.org/abs/2601.00001v2",
                "parse_warnings": [],
            },
        )
        write_frontmatter(
            workspace / "sources" / "normalized" / "github--acme-tool-v1.2.3.md",
            {
                "type": "normalized_source",
                "source_id": "github:acme-tool-v1.2.3",
                "source_kind": "code_archive",
                "status": "content_extracted",
                "raw_paths": ["raw/code/github-acme-tool-v1.2.3.tar.gz"],
                "manifest_path": "sources/manifest.jsonl",
                "parse_warnings": [],
            },
        )
        write_frontmatter(
            workspace / "sources" / "normalized" / "web--bare-secondary.md",
            {
                "type": "normalized_source",
                "source_id": "web:bare-secondary",
                "source_kind": "html",
                "status": "content_extracted",
                "raw_paths": ["raw/web/bare-secondary.html"],
                "manifest_path": "sources/manifest.jsonl",
                "url": "https://blog.example/current-fee",
                "parse_warnings": [],
            },
        )

        (workspace / "sources" / "discovery" / "candidates.jsonl").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-vendor",
                    "provider": "search",
                    "url": "https://docs.vendor.example/product/spec",
                    "title": "Official product spec",
                    "source_type": "web_page",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "fetch",
                    "status": "selected",
                    "selected_request_id": "req-vendor-spec",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (workspace / "sources" / "jurisdictions.yml").write_text(
            yaml.safe_dump(
                {
                    "jurisdiction_profiles": [
                        {
                            "jurisdiction_id": "es-social-security",
                            "name": "Spain Social Security",
                            "country": "es",
                            "official_domains": ["seg-social.es", "boe.es"],
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

        coverage = {
            "schema_version": "1.0",
            "question_slug": "vendor-product-spec",
            "created_at": "2026-06-29T00:00:00Z",
            "updated_at": "2026-06-29T00:00:00Z",
            "coverage_profile": "vendor-product-spec",
            "coverage_verdict": "pass",
            "required_facets": [
                {
                    "facet_id": "official-spec",
                    "description": "Confirm the product specification from an official vendor page.",
                    "required": True,
                    "evidence_path": "vendor_product_spec",
                    "source_policy": "official_vendor",
                    "freshness_policy": "current_product_spec",
                    "identity_policy": "origin_url_matches_candidate",
                    "min_sources": 1,
                    "accepted_source_ids": ["web:vendor-official-product-spec"],
                    "blocking_request_ids": [],
                    "facet_verdict": "pass",
                }
            ],
            "optional_facets": [],
        }
        (workspace / "sources" / "coverage" / "vendor-product-spec.yml").write_text(
            yaml.safe_dump(coverage, sort_keys=False),
            encoding="utf-8",
        )
        return workspace

    def load_inputs(self, workspace: Path):
        helper = load_policy_helper()
        return helper, helper.load_policy_inputs(workspace)

    def minimal_inputs(
        self,
        *,
        record: dict[str, Any] | None = None,
        normalized: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ):
        helper = load_policy_helper()
        source_id = "paper:source-value"
        return helper, source_id, helper.PolicyInputs(
            project_root=Path("."),
            config={},
            manifest_records={source_id: record or {"id": source_id}},
            normalized_records={source_id: normalized or {}},
            provenance_by_source_id={source_id: provenance or {}},
            candidates=[],
            candidates_by_request_id={},
            jurisdiction_profiles={},
            coverage_manifests={},
            warnings=[],
        )

    def test_source_value_skips_empty_normalized_containers_for_provenance_backfill(self):
        helper, source_id, inputs = self.minimal_inputs(
            normalized={"authors": []},
            provenance={"authors": ["Ada Lovelace"]},
        )

        self.assertEqual(["Ada Lovelace"], helper.source_value(inputs, source_id, ("authors",)))

    def test_source_value_returns_none_when_all_layers_are_empty_containers(self):
        helper, source_id, inputs = self.minimal_inputs(
            record={"id": "paper:source-value", "authors": []},
            normalized={"authors": []},
            provenance={"authors": []},
        )

        self.assertIsNone(helper.source_value(inputs, source_id, ("authors",)))

    def test_source_value_keeps_non_empty_normalized_value_precedence(self):
        helper, source_id, inputs = self.minimal_inputs(
            normalized={"authors": ["Grace Hopper"]},
            provenance={"authors": ["Ada Lovelace"]},
        )

        self.assertEqual(["Grace Hopper"], helper.source_value(inputs, source_id, ("authors",)))

    def test_source_date_metadata_falls_back_past_empty_normalized_mapping(self):
        helper, source_id, inputs = self.minimal_inputs(
            normalized={"date_metadata": {}},
            provenance={"date_metadata": {"valid_for_year": 2026}},
        )

        self.assertEqual({"valid_for_year": 2026}, helper.source_date_metadata(inputs, source_id))

    def update_sidecar(self, workspace: Path, relative_path: str, updates: dict[str, Any]) -> None:
        path = workspace / relative_path
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, dict):
            raise AssertionError(f"{relative_path} must contain a YAML mapping")
        document.update(updates)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def append_candidate(self, workspace: Path, candidate: dict[str, Any]) -> None:
        candidates = workspace / "sources" / "discovery" / "candidates.jsonl"
        with candidates.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(candidate, sort_keys=True) + "\n")

    def declare_pack_policy(self, workspace: Path) -> None:
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config["domain_pack"] = {
            "name": "general-science",
            "policy_vocabularies": {
                "freshness_policy": {
                    "pack:general-science/study-recency": (
                        "Require a reviewer to confirm study recency for this scientific question."
                    )
                }
            },
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def add_academic_source(self, workspace: Path, source_id: str, frontmatter: dict[str, Any]) -> None:
        manifest = workspace / "sources" / "manifest.jsonl"
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "paper",
                        "raw_paths": [f"raw/papers/{source_id.replace(':', '-')}.md"],
                        "status": "normalized",
                        "detected_at": "2026-06-29T00:00:00Z",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        write_frontmatter(
            workspace / "sources" / "normalized" / f"{source_id.replace(':', '--')}.md",
            {
                "type": "normalized_source",
                "source_id": source_id,
                "source_kind": "paper",
                "status": "content_extracted",
                "raw_paths": [f"raw/papers/{source_id.replace(':', '-')}.md"],
                "manifest_path": "sources/manifest.jsonl",
                "parse_warnings": [],
                **frontmatter,
            },
        )

    def add_github_source(
        self,
        workspace: Path,
        source_id: str,
        *,
        artifact_kind: str,
        provenance_updates: dict[str, Any] | None = None,
    ) -> None:
        raw_path = f"raw/code/{source_id.replace(':', '-')}.json"
        provenance = {
            "origin_url": "https://github.com/acme/tool",
            "repository_owner": "acme",
            "repository_name": "tool",
            "repository_full_name": "acme/tool",
            "repository_artifact_kind": artifact_kind,
            "repository_ref": "main",
            "commit_sha": "b" * 40,
            "license": "MIT",
            "retrieved_at": "2026-06-29T00:00:00Z",
            "checksum": "sha256:" + "2" * 64,
            "checksum_verified": True,
        }
        if artifact_kind == "source_archive":
            provenance["downloaded_archive_url"] = "https://api.github.com/repos/acme/tool/tarball/main"
            raw_path = f"raw/code/{source_id.replace(':', '-')}.tar.gz"
        if artifact_kind == "release_metadata":
            provenance["origin_url"] = "https://github.com/acme/tool/releases/tag/main"
        if provenance_updates:
            for key, value in provenance_updates.items():
                if value == "__delete__":
                    provenance.pop(key, None)
                else:
                    provenance[key] = value
        manifest = workspace / "sources" / "manifest.jsonl"
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "code_archive" if artifact_kind == "source_archive" else "json",
                        "raw_paths": [raw_path],
                        "status": "normalized",
                        "detected_at": "2026-06-29T00:00:00Z",
                        "provenance": provenance,
                    },
                    sort_keys=True,
                )
                + "\n"
            )

    def add_standards_source(
        self,
        workspace: Path,
        source_id: str,
        *,
        standards: dict[str, Any],
        provenance_updates: dict[str, Any] | None = None,
        candidate_updates: dict[str, Any] | None = None,
    ) -> None:
        raw_name = f"{source_id.replace(':', '-')}.html"
        raw_path = f"raw/web/{raw_name}"
        (workspace / raw_path).write_text("<html>Standards registry fixture.</html>\n", encoding="utf-8")
        provenance = {
            "origin_url": standards.get("registry_url", "https://standards.example/registry"),
            "retrieved_at": "2026-07-07T12:00:00Z",
            "retrieved_by": "fixture",
            "license": None,
            "terms_url": "https://standards.example/terms",
            "source_type": "standards_registry_entry",
            "standards": standards,
        }
        if provenance_updates:
            for key, value in provenance_updates.items():
                if value == "__delete__":
                    provenance.pop(key, None)
                else:
                    provenance[key] = value
        (workspace / f"{raw_path}.provenance.yml").write_text(
            yaml.safe_dump(provenance, sort_keys=False),
            encoding="utf-8",
        )
        manifest = workspace / "sources" / "manifest.jsonl"
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": source_id,
                        "kind": "html",
                        "raw_paths": [raw_path],
                        "status": "normalized",
                        "detected_at": "2026-07-07T12:00:00Z",
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        candidate = {
            "schema_version": "1.0",
            "candidate_id": f"cand-{source_id.replace(':', '-')}",
            "provider": "standards",
            "url": standards.get("registry_url", "https://standards.example/registry"),
            "title": standards.get("title", "Standards registry fixture"),
            "source_type": provenance.get("source_type", "standards_registry_entry"),
            "trust_tier": "official_primary",
            "official_source": True,
            "recommended_action": "fetch",
            "status": "fetched",
            "selected_source_id": source_id,
            "standards": standards,
        }
        if candidate_updates:
            candidate.update(candidate_updates)
        self.append_candidate(workspace, candidate)

    def test_load_policy_inputs_collects_local_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            helper, inputs = self.load_inputs(workspace)

        self.assertTrue(hasattr(helper, "PolicyInputs"))
        self.assertTrue(hasattr(helper, "PolicyResult"))
        self.assertIn("web:seg-social-cuota", inputs.manifest_records)
        self.assertEqual("10.5555/turboquant", inputs.normalized_records["paper:turboquant"]["doi"])
        self.assertEqual(
            "https://seg-social.es/wps/cuota-reducida",
            inputs.provenance_by_source_id["web:seg-social-cuota"]["origin_url"],
        )
        self.assertEqual("cand-vendor", inputs.candidates_by_request_id["req-vendor-spec"][0]["candidate_id"])
        self.assertIn("es-social-security", inputs.jurisdiction_profiles)
        self.assertIn("vendor-product-spec", inputs.coverage_manifests)

    def test_load_policy_inputs_indexes_canonical_selected_for_request_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.append_candidate(
                workspace,
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-new-selection-link",
                    "provider": "search",
                    "url": "https://example.org/candidate",
                    "title": "Canonical selected link",
                    "source_type": "web_page",
                    "status": "selected",
                    "selected_for_request_id": "req-new-canonical",
                },
            )
            _, inputs = self.load_inputs(workspace)

        self.assertEqual(
            "cand-new-selection-link",
            inputs.candidates_by_request_id["req-new-canonical"][0]["candidate_id"],
        )

    def test_official_legal_source_passes_source_and_identity_from_jurisdiction_domain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        source = helper.evaluate_source_policy("official_primary", ["web:seg-social-cuota"], inputs)
        identity = helper.evaluate_identity_policy("official_domain_match", ["web:seg-social-cuota"], inputs)

        self.assertEqual("pass", source.verdict)
        self.assertEqual("pass", identity.verdict)
        self.assertTrue(any("official domain" in reason.lower() for reason in identity.reasons))

    def test_non_legal_official_profile_passes_without_using_transport_allowlist_as_trust(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            config_path = workspace / "research.yml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8")
                + "  acquisition:\n"
                + "    web:\n"
                + "      allowed_domains:\n"
                + "        - epa.gov\n"
                + "        - allowed-only.example\n",
                encoding="utf-8",
            )
            manifest = workspace / "sources" / "manifest.jsonl"
            records = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
            records.extend(
                [
                    {
                        "id": "web:epa-guidance",
                        "kind": "html",
                        "raw_paths": ["raw/web/epa-guidance.html"],
                        "status": "normalized",
                        "detected_at": "2026-07-04T12:00:00Z",
                        "provenance": {
                            "origin_url": "https://epa.gov/battery/best-practices",
                            "retrieved_at": "2026-07-04T12:00:00Z",
                        },
                    },
                    {
                        "id": "web:allowed-only",
                        "kind": "html",
                        "raw_paths": ["raw/web/allowed-only.html"],
                        "status": "normalized",
                        "detected_at": "2026-07-04T12:00:00Z",
                        "provenance": {
                            "origin_url": "https://allowed-only.example/guidance",
                            "retrieved_at": "2026-07-04T12:00:00Z",
                        },
                    },
                ]
            )
            manifest.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))
            jurisdictions_path = workspace / "sources" / "jurisdictions.yml"
            jurisdictions = yaml.safe_load(jurisdictions_path.read_text(encoding="utf-8"))
            jurisdictions["jurisdiction_profiles"].append(
                {
                    "jurisdiction_id": "public-safety-authorities",
                    "name": "Public safety authorities",
                    "official_domains": ["epa.gov", "usfa.fema.gov"],
                    "blocked_domains": [],
                }
            )
            jurisdictions_path.write_text(yaml.safe_dump(jurisdictions, sort_keys=False), encoding="utf-8")
            helper, inputs = self.load_inputs(workspace)

        source = helper.evaluate_source_policy("official_primary", ["web:epa-guidance"], inputs)
        identity = helper.evaluate_identity_policy("official_domain_match", ["web:epa-guidance"], inputs)
        allowlist_source = helper.evaluate_source_policy("official_primary", ["web:allowed-only"], inputs)
        allowlist_identity = helper.evaluate_identity_policy("official_domain_match", ["web:allowed-only"], inputs)

        self.assertEqual("pass", source.verdict)
        self.assertEqual("pass", identity.verdict)
        self.assertNotEqual("pass", allowlist_source.verdict)
        self.assertNotEqual("pass", allowlist_identity.verdict)

    def test_academic_source_passes_index_and_citation_identity_from_normalized_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        indexed = helper.evaluate_source_policy("academic_indexed", ["paper:turboquant"], inputs)
        open_index = helper.evaluate_source_policy("openalex_or_arxiv", ["paper:turboquant"], inputs)
        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:turboquant"], inputs)

        self.assertEqual("pass", indexed.verdict)
        self.assertEqual("pass", open_index.verdict)
        self.assertEqual("pass", identity.verdict)

    def test_arxiv_citation_identity_accepts_valid_local_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:2601.00002",
                {
                    "title": "Unversioned ArXiv Identity",
                    "authors": ["Ada Lovelace"],
                    "year": 2026,
                    "arxiv_id": "arxiv:2601.00002",
                    "url": "https://arxiv.org/abs/2601.00002",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:2601.00002"], inputs)
        open_index = helper.evaluate_source_policy("openalex_or_arxiv", ["paper:2601.00002"], inputs)

        self.assertEqual("pass", identity.verdict)
        self.assertEqual("pass", open_index.verdict)
        self.assertTrue(any("arxiv_id" in reason for reason in identity.reasons))

    def test_openalex_citation_identity_accepts_valid_work_id_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:openalex-fixture",
                {
                    "title": "OpenAlex Identity Fixture",
                    "authors": ["Grace Hopper"],
                    "publication_year": 2025,
                    "openalex_id": "https://openalex.org/W260100001",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:openalex-fixture"], inputs)
        open_index = helper.evaluate_source_policy("openalex_or_arxiv", ["paper:openalex-fixture"], inputs)

        self.assertEqual("pass", identity.verdict)
        self.assertEqual("pass", open_index.verdict)
        self.assertTrue(any("openalex_id" in reason for reason in identity.reasons))

    def test_doi_only_citation_identity_accepts_valid_title_and_year(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:doi-only",
                {
                    "title": "DOI Only Identity Fixture",
                    "date": "2024-04-05",
                    "doi": "https://doi.org/10.5555/doi-only",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        indexed = helper.evaluate_source_policy("academic_indexed", ["paper:doi-only"], inputs)
        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:doi-only"], inputs)

        self.assertEqual("pass", indexed.verdict)
        self.assertEqual("pass", identity.verdict)
        self.assertTrue(any("doi" in reason for reason in identity.reasons))

    def test_citation_identity_fails_malformed_identifiers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:malformed-identifiers",
                {
                    "title": "Malformed Identifier Fixture",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "doi": "not-a-doi",
                    "arxiv_id": "not-arxiv",
                    "openalex_id": "not-openalex",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        indexed = helper.evaluate_source_policy("academic_indexed", ["paper:malformed-identifiers"], inputs)
        open_index = helper.evaluate_source_policy("openalex_or_arxiv", ["paper:malformed-identifiers"], inputs)
        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:malformed-identifiers"], inputs)

        self.assertEqual("manual_review", indexed.verdict)
        self.assertEqual("manual_review", open_index.verdict)
        self.assertEqual("fail", identity.verdict)
        self.assertTrue(any("malformed" in reason.lower() for reason in identity.reasons))

    def test_citation_identity_fails_missing_title_even_with_valid_identifier(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:missing-title",
                {
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "doi": "10.5555/missing-title",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_identity_policy("citation_id_resolves", ["paper:missing-title"], inputs)

        self.assertEqual("fail", identity.verdict)
        self.assertTrue(any("title" in reason.lower() for reason in identity.reasons))

    def test_citation_identity_fails_when_any_accepted_source_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_academic_source(
                workspace,
                "paper:valid-doi",
                {
                    "title": "Valid DOI Source",
                    "publication_year": 2026,
                    "doi": "10.5555/valid-doi",
                },
            )
            self.add_academic_source(
                workspace,
                "paper:invalid-doi",
                {
                    "title": "Invalid DOI Source",
                    "publication_year": 2026,
                    "doi": "invalid-doi",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_identity_policy(
            "citation_id_resolves",
            ["paper:valid-doi", "paper:invalid-doi"],
            inputs,
        )

        self.assertEqual("fail", identity.verdict)
        self.assertIn("paper:valid-doi", " ".join(identity.reasons))
        self.assertIn("paper:invalid-doi", " ".join(identity.reasons))

    def test_repository_source_passes_canonical_repository_release_snapshot_and_ref_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        source = helper.evaluate_source_policy("canonical_repository", ["github:acme-tool-v1.2.3"], inputs)
        freshness = helper.evaluate_freshness_policy("release_snapshot", ["github:acme-tool-v1.2.3"], inputs)
        identity = helper.evaluate_identity_policy("repo_ref_resolves", ["github:acme-tool-v1.2.3"], inputs)

        self.assertEqual("pass", source.verdict)
        self.assertEqual("pass", freshness.verdict)
        self.assertEqual("pass", identity.verdict)

    def test_github_archive_snapshot_passes_default_implementation_facet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        facet = {
            "facet_id": "implementation",
            "required": True,
            "evidence_path": "github_implementation",
            "source_policy": "canonical_repository",
            "freshness_policy": "release_snapshot",
            "identity_policy": "repo_ref_resolves",
            "accepted_source_ids": ["github:acme-tool-v1.2.3"],
        }
        results = helper.evaluate_facet_policies(facet, inputs)

        self.assertEqual(["pass", "pass", "pass"], [result.verdict for result in results])
        self.assertTrue(any("source_archive" in reason for result in results for reason in result.reasons))

    def test_declared_pack_freshness_policy_requires_manual_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.declare_pack_policy(workspace)
            helper, inputs = self.load_inputs(workspace)

        result = helper.evaluate_freshness_policy(
            "pack:general-science/study-recency",
            ["paper:turboquant"],
            inputs,
        )

        self.assertEqual("manual_review", result.verdict)
        self.assertTrue(any("study recency" in reason for reason in result.reasons))

    def test_undeclared_pack_freshness_policy_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.declare_pack_policy(workspace)
            helper, inputs = self.load_inputs(workspace)

        result = helper.evaluate_freshness_policy(
            "pack:general-science/not-declared",
            ["paper:turboquant"],
            inputs,
        )

        self.assertEqual("fail", result.verdict)
        self.assertTrue(any("not declared" in reason for reason in result.reasons))

    def test_github_metadata_snapshot_fails_default_implementation_facet(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_github_source(workspace, "github:metadata-only", artifact_kind="repository_metadata")
            helper, inputs = self.load_inputs(workspace)

        facet = {
            "facet_id": "implementation",
            "required": True,
            "evidence_path": "github_implementation",
            "source_policy": "canonical_repository",
            "freshness_policy": "release_snapshot",
            "identity_policy": "repo_ref_resolves",
            "accepted_source_ids": ["github:metadata-only"],
        }
        results = helper.evaluate_facet_policies(facet, inputs)

        self.assertEqual(["fail", "fail", "fail"], [result.verdict for result in results])
        self.assertTrue(any("not allowed" in reason.lower() for result in results for reason in result.reasons))

    def test_github_metadata_snapshot_passes_when_facet_explicitly_allows_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_github_source(workspace, "github:metadata-only", artifact_kind="repository_metadata")
            helper, inputs = self.load_inputs(workspace)

        facet = {
            "facet_id": "repository-metadata",
            "required": False,
            "evidence_path": "github_implementation",
            "source_policy": "canonical_repository",
            "freshness_policy": "release_snapshot",
            "identity_policy": "repo_ref_resolves",
            "accepted_source_ids": ["github:metadata-only"],
            "accepted_artifact_kinds": ["repository_metadata"],
        }
        results = helper.evaluate_facet_policies(facet, inputs)

        self.assertEqual(["pass", "pass", "pass"], [result.verdict for result in results])

    def test_github_identity_fails_missing_ref_missing_license_field_and_refusal_status(self):
        cases = [
            ("github:missing-ref", {"repository_ref": "__delete__"}, "ref"),
            ("github:missing-license-field", {"license": "__delete__"}, "license"),
            (
                "github:oversize-refusal",
                {"source_status": "refused_oversize", "notes": "GitHub archive was refused as oversized."},
                "refused",
            ),
        ]
        for source_id, updates, expected_reason in cases:
            with self.subTest(source_id=source_id):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.write_workspace(Path(tmpdir))
                    self.add_github_source(
                        workspace,
                        source_id,
                        artifact_kind="source_archive",
                        provenance_updates=updates,
                    )
                    helper, inputs = self.load_inputs(workspace)

                identity = helper.evaluate_identity_policy("repo_ref_resolves", [source_id], inputs)

                self.assertEqual("fail", identity.verdict)
                self.assertTrue(any(expected_reason in reason.lower() for reason in identity.reasons))

    def test_github_identity_accepts_explicit_unknown_license(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_github_source(
                workspace,
                "github:unknown-license",
                artifact_kind="source_archive",
                provenance_updates={"license": None},
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_identity_policy("repo_ref_resolves", ["github:unknown-license"], inputs)

        self.assertEqual("pass", identity.verdict)
        self.assertTrue(any("license status recorded" in reason.lower() for reason in identity.reasons))

    def test_vendor_source_passes_official_vendor_and_origin_matches_selected_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        source = helper.evaluate_source_policy("official_vendor", ["web:vendor-official-product-spec"], inputs)
        identity = helper.evaluate_identity_policy(
            "origin_url_matches_candidate",
            ["web:vendor-official-product-spec"],
            inputs,
        )

        self.assertEqual("pass", source.verdict)
        self.assertEqual("pass", identity.verdict)

    def test_missing_sources_fail_and_insufficient_metadata_requires_manual_review(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        missing = helper.evaluate_source_policy("official_primary", ["web:missing"], inputs)
        insufficient = helper.evaluate_source_policy("official_primary", ["web:bare-secondary"], inputs)

        self.assertEqual("fail", missing.verdict)
        self.assertEqual(["web:missing"], missing.source_ids)
        self.assertEqual("manual_review", insufficient.verdict)
        self.assertTrue(insufficient.remediation)

    def test_manual_review_policies_never_auto_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        source = helper.evaluate_source_policy("manual_review_required", ["web:seg-social-cuota"], inputs)
        freshness = helper.evaluate_freshness_policy("manual_review", ["web:seg-social-cuota"], inputs)

        self.assertEqual("manual_review", source.verdict)
        self.assertEqual("manual_review", freshness.verdict)

    def test_current_legal_figure_passes_with_covering_validity_period(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/seg-social-cuota.html.provenance.yml",
                {"validity_period": "2026-01-01/2026-12-31"},
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy("current_legal_figure", ["web:seg-social-cuota"], inputs)

        self.assertEqual("pass", freshness.verdict)
        self.assertTrue(any("validity period" in reason.lower() for reason in freshness.reasons))

    def test_currentness_date_uses_utc_day_not_display_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, _inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        self.assertEqual(
            date(2025, 12, 31),
            helper.date_from_value("2026-01-01T00:30:00+02:00"),
        )
        self.assertEqual(
            helper.date_from_value("2026-11-01T01:45:00-04:00"),
            helper.date_from_value("2026-11-01T05:45:00Z"),
        )

    def test_current_legal_figure_passes_with_date_metadata_valid_for_year(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/seg-social-cuota.html.provenance.yml",
                {"date_metadata": {"valid_for_year": 2026}},
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy("current_legal_figure", ["web:seg-social-cuota"], inputs)

        self.assertEqual("pass", freshness.verdict)
        self.assertTrue(any("date_metadata.valid_for_year" in reason for reason in freshness.reasons))

    def test_current_legal_figure_fails_stale_validity_period(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/seg-social-cuota.html.provenance.yml",
                {"validity_period": "2023-01-01/2025-12-31"},
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy("current_legal_figure", ["web:seg-social-cuota"], inputs)

        self.assertEqual("fail", freshness.verdict)
        self.assertTrue(any("stale" in reason.lower() for reason in freshness.reasons))

    def test_current_legal_figure_fails_superseded_candidate_risk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/seg-social-cuota.html.provenance.yml",
                {"validity_period": "2026-01-01/"},
            )
            self.append_candidate(
                workspace,
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-stale-law",
                    "provider": "legal",
                    "url": "https://seg-social.es/wps/cuota-reducida",
                    "title": "Historical reduced fee",
                    "source_type": "official_legal",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "review",
                    "status": "selected",
                    "reasoning": {"risk_flags": ["superseded_or_historical"]},
                },
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy("current_legal_figure", ["web:seg-social-cuota"], inputs)

        self.assertEqual("fail", freshness.verdict)
        self.assertTrue(any("superseded_or_historical" in reason for reason in freshness.reasons))

    def test_currentness_policies_fail_missing_date_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        legal = helper.evaluate_freshness_policy("current_legal_figure", ["web:seg-social-cuota"], inputs)
        product = helper.evaluate_freshness_policy("current_product_spec", ["web:vendor-official-product-spec"], inputs)

        self.assertEqual("fail", legal.verdict)
        self.assertEqual("fail", product.verdict)
        self.assertTrue(any("date" in reason.lower() for reason in legal.reasons))
        self.assertTrue(any("date" in reason.lower() for reason in product.reasons))

    def test_current_product_spec_passes_with_date_not_available_note(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/vendor-product.html.provenance.yml",
                {"date_not_available": "Official vendor spec page exposes no publication date."},
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy(
            "current_product_spec",
            ["web:vendor-official-product-spec"],
            inputs,
        )

        self.assertEqual("pass", freshness.verdict)
        self.assertTrue(any("date_not_available" in reason for reason in freshness.reasons))

    def test_official_error_page_fails_current_product_spec(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/vendor-product.html.provenance.yml",
                {
                    "date_not_available": "Official vendor spec page exposes no publication date.",
                    "source_status": "error_page",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        freshness = helper.evaluate_freshness_policy(
            "current_product_spec",
            ["web:vendor-official-product-spec"],
            inputs,
        )

        self.assertEqual("fail", freshness.verdict)
        self.assertTrue(any("error_page" in reason for reason in freshness.reasons))

    def test_unusable_vendor_source_fails_all_policy_checks_even_with_valid_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.update_sidecar(
                workspace,
                "raw/web/vendor-product.html.provenance.yml",
                {
                    "date_not_available": "Official vendor spec page exposes no publication date.",
                    "source_status": "unavailable",
                    "delivery_failure_code": "tls_verification_failed",
                    "delivery_failure_detail": "TLS certificate-chain validation failed during capture.",
                },
            )
            helper, inputs = self.load_inputs(workspace)

        source = helper.evaluate_source_policy("official_vendor", ["web:vendor-official-product-spec"], inputs)
        freshness = helper.evaluate_freshness_policy(
            "current_product_spec",
            ["web:vendor-official-product-spec"],
            inputs,
        )
        identity = helper.evaluate_identity_policy(
            "origin_url_matches_candidate",
            ["web:vendor-official-product-spec"],
            inputs,
        )

        self.assertEqual(["fail", "fail", "fail"], [source.verdict, freshness.verdict, identity.verdict])
        for result in (source, freshness, identity):
            reason_text = "\n".join(result.reasons)
            self.assertIn("source_status:unavailable", reason_text)
            self.assertIn("delivery_failure_code:tls_verification_failed", reason_text)

    def test_standards_registry_policies_pass_exact_iso_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_standards_source(
                workspace,
                "web:iso-19131",
                standards={
                    "registry_provider": "iso-open-data",
                    "standards_body": "ISO",
                    "designation": "ISO 19131:2022",
                    "title": "Geographic information - Data product specifications",
                    "edition": 2,
                    "publication_date": "2022-11-01",
                    "status": "published",
                    "registry_url": "https://www.iso.org/standard/77442.html",
                    "dataset_license": "ODC-BY-1.0",
                },
                provenance_updates={"terms_url": "https://www.iso.org/open-data.html"},
            )
            helper, inputs = self.load_inputs(workspace)

        source = helper.evaluate_source_policy("official_standards_registry", ["web:iso-19131"], inputs)
        body = helper.evaluate_source_policy("standards_body_primary", ["web:iso-19131"], inputs)
        freshness = helper.evaluate_freshness_policy("current_standard_reference", ["web:iso-19131"], inputs)
        identity = helper.evaluate_facet_policies(
            {
                "facet_id": "iso-identity",
                "source_policy": "official_standards_registry",
                "freshness_policy": "current_standard_reference",
                "identity_policy": "standard_designation_matches_registry",
                "accepted_source_ids": ["web:iso-19131"],
                "standard_designation": "ISO 19131:2022",
                "standard_edition": 2,
                "standard_title": "Geographic information - Data product specifications",
            },
            inputs,
        )[2]

        self.assertEqual("pass", source.verdict)
        self.assertEqual("pass", body.verdict)
        self.assertEqual("pass", freshness.verdict)
        self.assertEqual("pass", identity.verdict)

    def test_standards_source_policies_fail_unknown_terms_and_non_body_host(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_standards_source(
                workspace,
                "web:mirrored-iso",
                standards={
                    "registry_provider": "iso-open-data",
                    "standards_body": "ISO",
                    "designation": "ISO 19131:2022",
                    "title": "Geographic information - Data product specifications",
                    "edition": 2,
                    "status": "published",
                    "registry_url": "https://mirror.example/iso-19131",
                },
                provenance_updates={"terms_url": "__delete__"},
                candidate_updates={"official_source": False, "trust_tier": "secondary"},
            )
            helper, inputs = self.load_inputs(workspace)

        official = helper.evaluate_source_policy("official_standards_registry", ["web:mirrored-iso"], inputs)
        body = helper.evaluate_source_policy("standards_body_primary", ["web:mirrored-iso"], inputs)

        self.assertEqual("fail", official.verdict)
        self.assertTrue(any("registry_terms_unknown" in reason for reason in official.reasons))
        self.assertEqual("fail", body.verdict)
        self.assertTrue(any("standard_reference_missing" in reason for reason in body.reasons))

    def test_current_standard_reference_fails_terminal_statuses(self):
        cases = [
            ("web:withdrawn-standard", "withdrawn", "standard_status_withdrawn"),
            ("web:superseded-standard", "superseded", "standard_status_superseded"),
            ("web:draft-standard", "draft", "standard_status_draft"),
        ]
        for source_id, status, expected_reason in cases:
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    workspace = self.write_workspace(Path(tmpdir))
                    self.add_standards_source(
                        workspace,
                        source_id,
                        standards={
                            "registry_provider": "iso-open-data",
                            "standards_body": "ISO",
                            "designation": "ISO 19131:2022",
                            "title": "Geographic information - Data product specifications",
                            "edition": 2,
                            "status": status,
                            "registry_url": "https://www.iso.org/standard/77442.html",
                            "dataset_license": "ODC-BY-1.0",
                            "replaced_by": "ISO 19131:2022" if status == "superseded" else None,
                        },
                        provenance_updates={"terms_url": "https://www.iso.org/open-data.html"},
                    )
                    helper, inputs = self.load_inputs(workspace)

                freshness = helper.evaluate_freshness_policy("current_standard_reference", [source_id], inputs)

                self.assertEqual("fail", freshness.verdict)
                self.assertTrue(any(expected_reason in reason for reason in freshness.reasons), freshness.reasons)

    def test_standards_identity_fails_missing_edition_and_wrong_designation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_standards_source(
                workspace,
                "web:iso-missing-edition",
                standards={
                    "registry_provider": "iso-open-data",
                    "standards_body": "ISO",
                    "designation": "ISO 19131:2022",
                    "title": "Geographic information - Data product specifications",
                    "status": "published",
                    "registry_url": "https://www.iso.org/standard/77442.html",
                    "dataset_license": "ODC-BY-1.0",
                },
                provenance_updates={"terms_url": "https://www.iso.org/open-data.html"},
            )
            helper, inputs = self.load_inputs(workspace)

        identity = helper.evaluate_facet_policies(
            {
                "facet_id": "wrong-iso",
                "source_policy": "official_standards_registry",
                "freshness_policy": "current_standard_reference",
                "identity_policy": "standard_designation_matches_registry",
                "accepted_source_ids": ["web:iso-missing-edition"],
                "standard_designation": "ISO 19131:2007",
                "standard_edition": 2,
            },
            inputs,
        )[2]

        self.assertEqual("fail", identity.verdict)
        self.assertTrue(any("standard_edition_missing" in reason for reason in identity.reasons))
        self.assertTrue(any("standard_title_mismatch" in reason for reason in identity.reasons))

    def test_product_requirement_policy_distinguishes_guidance_from_ojeu_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir))
            self.add_standards_source(
                workspace,
                "web:your-europe-guidance",
                standards={
                    "registry_provider": "your-europe",
                    "product_category": "toy safety",
                    "designation": "EN 71-1:2014+A1:2018",
                    "status": "published",
                    "registry_url": "https://europa.eu/youreurope/business/product-requirements/toys/",
                    "publication_date": "2026-01-10",
                },
                provenance_updates={
                    "source_type": "product_requirement_guidance",
                    "terms_url": "https://europa.eu/terms",
                },
            )
            self.add_standards_source(
                workspace,
                "web:ojeu-toy-standard",
                standards={
                    "registry_provider": "ojeu",
                    "standards_body": "CEN",
                    "product_category": "toy safety",
                    "legal_act": "Directive 2009/48/EC",
                    "designation": "EN 71-1:2014+A1:2018",
                    "harmonised_standard_reference": "EN 71-1:2014+A1:2018",
                    "ojeu_reference": "OJ C 282/4",
                    "ojeu_reference_date": "2023-08-04",
                    "status": "published",
                    "registry_url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=OJ:C:2023:282:TOC",
                    "publication_date": "2023-08-04",
                },
                provenance_updates={"terms_url": "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html"},
            )
            helper, inputs = self.load_inputs(workspace)

        guidance_freshness = helper.evaluate_freshness_policy(
            "current_product_requirement",
            ["web:your-europe-guidance"],
            inputs,
        )
        ojeu_freshness = helper.evaluate_freshness_policy(
            "current_product_requirement",
            ["web:ojeu-toy-standard"],
            inputs,
        )
        ojeu_identity = helper.evaluate_facet_policies(
            {
                "facet_id": "toy-harmonised-standard",
                "source_policy": "official_standards_registry",
                "freshness_policy": "current_product_requirement",
                "identity_policy": "registry_entry_matches_product_requirement",
                "accepted_source_ids": ["web:ojeu-toy-standard"],
                "product_category": "toy safety",
                "legal_act": "Directive 2009/48/EC",
                "standard_designation": "EN 71-1:2014+A1:2018",
            },
            inputs,
        )[2]

        self.assertEqual("fail", guidance_freshness.verdict)
        self.assertTrue(
            any("product_requirement_guidance_not_legal_authority" in reason for reason in guidance_freshness.reasons)
        )
        self.assertEqual("pass", ojeu_freshness.verdict)
        self.assertEqual("pass", ojeu_identity.verdict)

    def test_evaluate_facet_and_coverage_manifest_returns_structured_policy_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            helper, inputs = self.load_inputs(self.write_workspace(Path(tmpdir)))

        manifest = inputs.coverage_manifests["vendor-product-spec"]
        facet = manifest["required_facets"][0]
        facet_results = helper.evaluate_facet_policies(facet, inputs)
        manifest_results = helper.evaluate_coverage_manifest_policies(manifest, inputs)

        self.assertEqual(
            ["official_vendor", "current_product_spec", "origin_url_matches_candidate"],
            [result.policy for result in facet_results],
        )
        self.assertEqual(["pass", "fail", "pass"], [result.verdict for result in facet_results])
        self.assertEqual("vendor-product-spec", manifest_results["question_slug"])
        self.assertEqual("official-spec", manifest_results["facets"][0]["facet_id"])
        self.assertEqual(
            {
                "policy": "official_vendor",
                "verdict": "pass",
                "source_ids": ["web:vendor-official-product-spec"],
                "reasons": facet_results[0].reasons,
                "remediation": facet_results[0].remediation,
            },
            manifest_results["facets"][0]["policy_results"][0],
        )


QUESTION_SLUG = "needs-quote-evidence"


class PackPolicyRuleTests(unittest.TestCase):
    """CR-9 T3: a pack's declared rules decide its own policies instead of queueing them."""

    def build_workspace(self, root: Path, *, rules: dict[str, Any] | None = None) -> Path:
        workspace = root / "pack-workspace"
        (workspace / "raw" / "data").mkdir(parents=True)
        (workspace / "sources" / "normalized").mkdir(parents=True)
        (workspace / "wiki" / "questions").mkdir(parents=True)
        (workspace / "research.yml").write_text(
            yaml.safe_dump(
                {
                    "project": {"name": "pack-rule-fixture"},
                    "sources": {
                        "manifest_path": "sources/manifest.jsonl",
                        "normalized_dir": "sources/normalized",
                        "coverage_dir": "sources/coverage",
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (workspace / "sources" / "manifest.jsonl").write_text("", encoding="utf-8")
        declare_pack(workspace, rules=rules)
        return workspace

    def write_question(self, workspace: Path, *, metadata: dict[str, Any] | None = None) -> None:
        frontmatter: dict[str, Any] = {"type": "question", "slug": QUESTION_SLUG, "status": "open"}
        if metadata is not None:
            frontmatter["metadata"] = metadata
        (workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md").write_text(
            "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# Question\n",
            encoding="utf-8",
        )

    def add_quote_source(self, workspace: Path, source_id: str, **kwargs: Any) -> str:
        return deliver_quote(workspace, source_id, **kwargs)

    def load_inputs(self, workspace: Path):
        helper = load_policy_helper()
        return helper, helper.load_policy_inputs(workspace)

    def reason_text(self, policy_result) -> str:
        return "\n".join(policy_result.reasons)

    def test_a_rule_for_an_id_declared_under_two_sections_is_refused(self):
        """The ambiguity is refused where it is written, not guessed at downstream.

        A facet names one policy per section, so a rule on an id declared under several
        cannot say which field it decides. Loading such a pack refuses rather than binding
        the rule to whichever section happened to be checked first.
        """
        shared = f"pack:{PACK_NAME}/registered-provider"
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["domain_pack"]["policy_vocabularies"] = {
                "source_policy": {shared: "Must come from a registered provider."},
                "identity_policy": {shared: "The quoted SKU must match the candidate."},
            }
            config["domain_pack"]["policy_rules"] = {
                shared: {"all_of": [{"one_of_provenance": {"providers": [PROVIDER_ID]}}]}
            }
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            helper = load_policy_helper()

            with self.assertRaises(helper.PolicyRuleError) as caught:
                helper.load_policy_inputs(workspace)

            self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
            self.assertIn("more than one rule-carrying vocabulary section", caught.exception.message)

    def test_a_rule_bound_to_another_section_decides_nothing(self):
        """Defence in depth behind the declaration refusal above.

        Nothing a pack can write reaches this now, but the lookup is by policy id, so the
        guard stays: a rule that does not belong to the field being decided must leave the
        policy on the review fallback rather than answering for it.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:matching", age_hours=2, sku=CANDIDATE_SKU)
            helper, inputs = self.load_inputs(workspace)
            bound = inputs.policy_rules[IDENTITY_POLICY]
            self.assertEqual("identity_policy", bound.section)

            # The same rule, mislabelled as deciding the freshness field.
            inputs.policy_rules[IDENTITY_POLICY] = replace(bound, section="freshness_policy")
            decided = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:matching"], inputs, question_slug=QUESTION_SLUG
            )

            self.assertEqual(helper.VERDICT_MANUAL_REVIEW, decided.verdict, self.reason_text(decided))
            self.assertIn(IDENTITY_DEFINITION, self.reason_text(decided))

    def test_a_discovery_candidate_is_never_a_delivered_provider_identity(self):
        """`one_of_provenance` asks who delivered the evidence, not who was proposed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={SOURCE_POLICY: SOURCE_RULE})
            source_id = self.add_quote_source(
                workspace, "quote:unattributed", age_hours=2, provider_id=None
            )
            origin = f"https://api.supplier.test/product/{CANDIDATE_SKU}"
            candidates = workspace / "sources" / "discovery"
            candidates.mkdir(parents=True, exist_ok=True)
            (candidates / "candidates.jsonl").write_text(
                json.dumps(
                    {
                        "candidate_id": "cand-1",
                        "source_id": source_id,
                        "url": origin,
                        "standards": {"registry_provider": PROVIDER_ID},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            helper, inputs = self.load_inputs(workspace)

            self.assertEqual((), helper.rule_provider_ids(inputs, source_id))
            decided = helper.evaluate_source_policy(SOURCE_POLICY, [source_id], inputs)
            self.assertEqual(helper.VERDICT_FAIL, decided.verdict, self.reason_text(decided))

    def test_the_questions_directory_follows_the_configured_wiki_root(self):
        """`wiki.root` is renameable, and every other reader honours it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["wiki"] = {"root": "knowledge"}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            moved = workspace / "knowledge" / "questions"
            moved.mkdir(parents=True)
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            (workspace / "wiki" / "questions" / f"{QUESTION_SLUG}.md").rename(moved / f"{QUESTION_SLUG}.md")
            self.add_quote_source(workspace, "quote:matching", age_hours=2, sku=CANDIDATE_SKU)
            helper, inputs = self.load_inputs(workspace)

            decided = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:matching"], inputs, question_slug=QUESTION_SLUG
            )
            self.assertEqual(helper.VERDICT_OK, decided.verdict, self.reason_text(decided))

    def test_a_path_shaped_slug_never_leaves_the_questions_directory(self):
        """`None`, not `{}`: an empty mapping reads as a question missing a key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            helper, inputs = self.load_inputs(workspace)
            self.assertIsNone(inputs.question_frontmatter_for("../../etc/passwd"))
            self.assertIsNone(inputs.question_frontmatter_for("/etc/hosts"))

    def test_an_unusable_wiki_root_fails_rules_closed_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            config_path = workspace / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["wiki"] = {"root": "../escape"}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            self.add_quote_source(workspace, "quote:a", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            self.assertIsNone(inputs.question_frontmatter_for(QUESTION_SLUG))
            decided = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:a"], inputs, question_slug=QUESTION_SLUG
            )
            self.assertEqual(helper.VERDICT_FAIL, decided.verdict, self.reason_text(decided))

    def test_pack_freshness_rule_decides_the_quote_instead_of_queueing_it(self):
        """The headline: 48h declared, a 50-hour quote fails, a fresh one passes, no review."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            self.add_quote_source(workspace, "quote:stale", age_hours=50)
            helper, inputs = self.load_inputs(workspace)

            fresh = helper.evaluate_freshness_policy(FRESHNESS_POLICY, ["quote:fresh"], inputs)
            stale = helper.evaluate_freshness_policy(FRESHNESS_POLICY, ["quote:stale"], inputs)

        self.assertEqual("pass", fresh.verdict)
        self.assertEqual("fail", stale.verdict)
        self.assertNotIn("manual_review", json.dumps(fresh.to_dict()))
        self.assertNotIn("manual_review", json.dumps(stale.to_dict()))
        self.assertIn("provenance/retrieved_at", self.reason_text(fresh))
        self.assertIn("2.0h old; max_age allows 48h.", self.reason_text(fresh))
        self.assertIn("rule_stale: quote:stale provenance/retrieved_at", self.reason_text(stale))
        self.assertIn("50.0h old; max_age allows 48h.", self.reason_text(stale))
        self.assertIn(FRESHNESS_POLICY, stale.remediation)

    def test_declared_pack_policy_without_a_rule_still_requires_manual_review(self):
        """The fallback this change must not disturb, pinned to its exact recorded text."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_freshness_policy(FRESHNESS_POLICY, ["quote:fresh"], inputs)

        self.assertEqual("manual_review", result.verdict)
        self.assertEqual(
            [f"Domain-pack policy {FRESHNESS_POLICY} requires recorded domain review: {FRESHNESS_DEFINITION}"],
            result.reasons,
        )
        self.assertEqual(
            "Review the accepted local source metadata and record a stronger source, provenance, or manual decision.",
            result.remediation,
        )

    def test_undeclared_namespaced_policy_still_fails_closed_alongside_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_freshness_policy(f"pack:{PACK_NAME}/never-declared", ["quote:fresh"], inputs)

        self.assertEqual("fail", result.verdict)
        self.assertIn("is not declared by the active domain pack", self.reason_text(result))

    def test_unusable_evidence_still_wins_over_a_rule_that_would_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:unusable", age_hours=2, usable=False)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_freshness_policy(FRESHNESS_POLICY, ["quote:unusable"], inputs)

        self.assertEqual("fail", result.verdict)
        self.assertIn("is marked unusable evidence", self.reason_text(result))
        self.assertNotIn("max_age", self.reason_text(result))

    def test_record_pointer_fails_closed_when_the_source_has_no_structured_view(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:no-view", age_hours=2, structured_view="absent")
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:no-view"], inputs, question_slug=QUESTION_SLUG
            )

        self.assertEqual("fail", result.verdict)
        self.assertIn("structured_view_missing: quote:no-view record/supplier_quote/sku", self.reason_text(result))

    def test_record_pointer_fails_closed_when_the_structured_view_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:tampered", age_hours=2, structured_view="corrupt")
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:tampered"], inputs, question_slug=QUESTION_SLUG
            )

        self.assertEqual("fail", result.verdict)
        self.assertIn("structured_view_corrupt: quote:tampered record/supplier_quote/sku", self.reason_text(result))

    def test_question_field_rule_passes_with_the_key_and_fails_without_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:matching", age_hours=2)
            helper, inputs = self.load_inputs(workspace)
            matched = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:matching"], inputs, question_slug=QUESTION_SLUG
            )

            self.write_question(workspace, metadata={"unrelated": "value"})
            _, reloaded = self.load_inputs(workspace)
            unkeyed = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:matching"], reloaded, question_slug=QUESTION_SLUG
            )

        self.assertEqual("pass", matched.verdict)
        self.assertIn("record/supplier_quote/sku", self.reason_text(matched))
        self.assertIn("was delivered by provider 'aliexpress-ds'", self.reason_text(matched))
        self.assertEqual("fail", unkeyed.verdict)
        self.assertIn("rule_field_unresolved: quote:matching question/metadata/candidate_sku", self.reason_text(unkeyed))

    def test_question_field_rule_without_a_threaded_slug_fails_rather_than_crashing(self):
        """A caller that predates the slug parameter gets a named refusal, not a traceback."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:matching", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_identity_policy(IDENTITY_POLICY, ["quote:matching"], inputs)

        self.assertEqual("fail", result.verdict)
        self.assertIn("rule_field_unresolved: quote:matching question/metadata/candidate_sku", self.reason_text(result))
        self.assertIn("the question carries no frontmatter mapping", self.reason_text(result))

    def test_one_of_provenance_never_reads_the_fetching_agent_as_a_provider(self):
        """`retrieved_by` holds `fetch_sources.py/aliexpress-ds`; matching on it would pass."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:agent-only", age_hours=2, provider_id=None)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:agent-only"], inputs, question_slug=QUESTION_SLUG
            )

        self.assertEqual("fail", result.verdict)
        self.assertIn("rule_provenance_not_allowed: quote:agent-only", self.reason_text(result))
        self.assertIn("has provider ids (none)", self.reason_text(result))

    def test_multi_source_facet_fails_listing_only_the_stale_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            self.add_quote_source(workspace, "quote:stale", age_hours=50)
            helper, inputs = self.load_inputs(workspace)

            results = helper.evaluate_facet_policies(
                {
                    "facet_id": "supplier-quote",
                    "source_policy": "none-of-your-business",
                    "freshness_policy": FRESHNESS_POLICY,
                    "identity_policy": "none",
                    "accepted_source_ids": ["quote:fresh", "quote:stale"],
                },
                inputs,
                question_slug=QUESTION_SLUG,
            )

        freshness = next(result for result in results if result.policy == FRESHNESS_POLICY)
        self.assertEqual("fail", freshness.verdict)
        self.assertEqual(["quote:fresh", "quote:stale"], freshness.source_ids)
        self.assertIn("quote:stale", self.reason_text(freshness))
        self.assertNotIn("quote:fresh", self.reason_text(freshness))

    def test_manual_review_required_keeps_a_passing_rule_in_the_review_queue(self):
        """A pack may automate the mechanical half and still insist on a human for the rest."""
        rule = {**FRESHNESS_RULE, "manual_review_required": True}
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: rule})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_freshness_policy(FRESHNESS_POLICY, ["quote:fresh"], inputs)

        self.assertEqual("manual_review", result.verdict)
        self.assertIn("satisfied every declared rule check", self.reason_text(result))
        self.assertIn(FRESHNESS_DEFINITION, self.reason_text(result))
        self.assertIn("max_age allows 48h.", self.reason_text(result))

    def test_an_injected_clock_decides_the_age_rather_than_wall_time(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            later = helper.evaluate_freshness_policy(
                FRESHNESS_POLICY,
                ["quote:fresh"],
                inputs,
                now=datetime.now(timezone.utc) + timedelta(hours=60),
            )

        self.assertEqual("fail", later.verdict)
        self.assertIn("62.0h old; max_age allows 48h.", self.reason_text(later))

    def test_a_rule_declared_for_one_field_cannot_decide_another(self):
        """The identity rule's id is not in the freshness vocabulary, so freshness refuses it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:matching", age_hours=2)
            helper, inputs = self.load_inputs(workspace)

            result = helper.evaluate_freshness_policy(
                IDENTITY_POLICY, ["quote:matching"], inputs, question_slug=QUESTION_SLUG
            )

        self.assertEqual("fail", result.verdict)
        self.assertIn("is not declared by the active domain pack", self.reason_text(result))

    def test_a_malformed_rule_refuses_the_whole_evaluation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                rules={FRESHNESS_POLICY: {"all_of": [{"max_age": {"field": "provenance/retrieved_at", "hours": -1}}]}},
            )
            helper = load_policy_helper()

            with self.assertRaises(helper._policy_primitives.PolicyRuleError) as caught:
                helper.load_policy_inputs(workspace)

        self.assertEqual("CONFIG_INVALID", caught.exception.error_code)
        self.assertIn("hours must be greater than zero", caught.exception.details["errors"][0])

    def test_a_rule_decided_over_no_present_source_reports_missing_rather_than_pass(self):
        """The evaluators refuse an all-missing set first; the helper refuses it again."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            helper, inputs = self.load_inputs(workspace)

            result = helper.pack_policy_result(
                FRESHNESS_POLICY,
                "freshness_policy",
                ["quote:absent"],
                [],
                ["quote:absent"],
                inputs,
                question_slug=None,
                now=None,
            )

        self.assertEqual("fail", result.verdict)
        self.assertIn("Missing accepted source id(s): quote:absent.", result.reasons)

    def test_the_facet_level_clock_reaches_every_policy_it_dispatches(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={FRESHNESS_POLICY: FRESHNESS_RULE})
            self.add_quote_source(workspace, "quote:fresh", age_hours=2)
            helper, inputs = self.load_inputs(workspace)
            facet = {
                "facet_id": "supplier-quote",
                "source_policy": "manual_review_required",
                "freshness_policy": FRESHNESS_POLICY,
                "identity_policy": "none",
                "accepted_source_ids": ["quote:fresh"],
            }

            now = helper.evaluate_facet_policies(facet, inputs)
            later = helper.evaluate_facet_policies(
                facet, inputs, now=datetime.now(timezone.utc) + timedelta(hours=60)
            )

        self.assertEqual("pass", next(r for r in now if r.policy == FRESHNESS_POLICY).verdict)
        self.assertEqual("fail", next(r for r in later if r.policy == FRESHNESS_POLICY).verdict)

    def test_the_structured_view_and_question_page_are_each_read_once(self):
        """The caches are what let a rules-free workspace pay nothing for this machinery."""
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), rules={IDENTITY_POLICY: IDENTITY_RULE})
            self.write_question(workspace, metadata={"candidate_sku": CANDIDATE_SKU})
            self.add_quote_source(workspace, "quote:matching", age_hours=2)
            helper, inputs = self.load_inputs(workspace)
            self.assertEqual({}, inputs.structured_view_loads)
            self.assertEqual({}, inputs.question_frontmatters)

            first = inputs.structured_view_for("quote:matching")
            helper.evaluate_identity_policy(
                IDENTITY_POLICY, ["quote:matching"], inputs, question_slug=QUESTION_SLUG
            )

        self.assertIs(first, inputs.structured_view_for("quote:matching"))
        self.assertEqual([QUESTION_SLUG], list(inputs.question_frontmatters))


if __name__ == "__main__":
    unittest.main()
