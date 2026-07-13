import importlib.util
import inspect
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
CODEBASE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "codebase-intake"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LINT = load_script_module("ingest_contract_lint", "lint.py")
INVENTORY = load_script_module("ingest_contract_inventory", "source_inventory.py")
NORMALIZE = load_script_module("ingest_contract_normalize", "normalize_sources.py")
QUERY = load_script_module("ingest_contract_query", "query_index.py")


class IngestContractTests(unittest.TestCase):
    def build_workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        for path in (
            "sources/normalized",
            "sources/cards",
            "wiki/sources",
            "wiki/concepts",
            "wiki/synthesis",
        ):
            (workspace / path).mkdir(parents=True, exist_ok=True)
        (workspace / "research.yml").write_text(
            "project:\n"
            "  name: ingest-contract\n"
            "  description: Deterministic ingest contract fixture.\n"
            "raw:\n"
            "  source_roots:\n"
            "    - raw/papers\n"
            "sources:\n"
            "  manifest_path: sources/manifest.jsonl\n"
            "  normalized_dir: sources/normalized\n"
            "  default_status: discovered\n"
            "  lifecycle_statuses:\n"
            "    - discovered\n"
            "    - normalized\n"
            "    - noted\n"
            "    - integrated\n"
            "    - deferred\n"
            "    - superseded\n"
            "    - rejected\n"
            "wiki:\n"
            "  root: wiki\n"
            "  required_dirs: []\n"
            "  allowed_page_types:\n"
            "    - source\n"
            "    - concept\n"
            "    - synthesis\n"
            "  frontmatter_required:\n"
            "    - type\n"
            "    - created\n"
            "    - updated\n"
            "    - source_ids\n"
            "  frontmatter_type_rules:\n"
            "    source:\n"
            "      field_types:\n"
            "        source_ids: string_list\n"
            "      non_empty_fields:\n"
            "        - source_ids\n"
            "    concept:\n"
            "      field_types:\n"
            "        source_ids: string_list\n"
            "      non_empty_fields:\n"
            "        - source_ids\n"
            "lint:\n"
            "  validate_structure: false\n"
            "  validate_frontmatter: true\n"
            "  validate_links: false\n"
            "  validate_source_coverage: true\n"
            "  validate_claims: false\n"
        )
        records = [
            {
                "id": "paper:contract-paper",
                "kind": "paper",
                "raw_paths": ["raw/papers/contract-paper.txt"],
                "status": "integrated",
                "detected_at": "2026-05-31T00:00:00Z",
            },
            {
                "id": "link:contract-link",
                "kind": "web_link",
                "url": "https://example.org/contract",
                "raw_paths": ["raw/links/contract.txt"],
                "status": "normalized",
                "detected_at": "2026-05-31T00:00:00Z",
            },
            {
                "id": "data:contract-card",
                "kind": "table",
                "raw_paths": ["raw/data/contract.csv"],
                "status": "deferred",
                "detected_at": "2026-05-31T00:00:00Z",
            },
            {
                "id": "codebase:contract-artifact",
                "kind": "codebase_architecture",
                "raw_paths": ["raw/code/contract"],
                "status": "rejected",
                "detected_at": "2026-05-31T00:00:00Z",
            },
            {
                "id": "paper:old-contract-paper",
                "kind": "paper",
                "raw_paths": ["raw/papers/old-contract-paper.txt"],
                "status": "superseded",
                "detected_at": "2026-05-31T00:00:00Z",
            },
        ]
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        )
        self.write_normalized(workspace, "paper:contract-paper", "paper", "Contract Paper")
        self.write_normalized(workspace, "link:contract-link", "web_link", "Contract Link")
        self.write_source_note(workspace, "paper-source.md", "paper:contract-paper", "integrated", "Contract Paper")
        self.write_source_note(workspace, "link-source.md", "link:contract-link", "noted", "Contract Link")
        self.write_source_note(workspace, "data-source.md", "data:contract-card", "deferred", "Contract Data Card")
        self.write_source_note(
            workspace,
            "codebase-source.md",
            "codebase:contract-artifact",
            "rejected",
            "Contract Codebase Artifact",
        )
        (workspace / "wiki" / "concepts" / "contract-concept.md").write_text(
            "---\n"
            "type: concept\n"
            "created: 2026-05-31\n"
            "updated: 2026-05-31\n"
            "source_ids:\n"
            "  - paper:contract-paper\n"
            "summary: Integrated contract concept.\n"
            "---\n\n"
            "# Contract Concept\n\n"
            "Cites `paper:contract-paper` from [[../sources/paper-source|the source note]].\n"
        )
        return workspace

    def write_normalized(self, workspace: Path, source_id: str, kind: str, title: str) -> None:
        filename = source_id.replace(":", "--") + ".md"
        (workspace / "sources" / "normalized" / filename).write_text(
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            f"source_kind: {kind}\n"
            "status: content_extracted\n"
            "created: 2026-05-31\n"
            "updated: 2026-05-31\n"
            "raw_paths: []\n"
            "manifest_path: sources/manifest.jsonl\n"
            f"title: {title}\n"
            "extraction_method: manual\n"
            "content_hash: sha256:contract\n"
            "---\n\n"
            f"# {title}\n\nExtracted contract fixture text.\n"
        )

    def write_source_note(self, workspace: Path, filename: str, source_id: str, status: str, title: str) -> None:
        normalized_path = f"sources/normalized/{source_id.replace(':', '--')}.md"
        (workspace / "wiki" / "sources" / filename).write_text(
            "---\n"
            "type: source\n"
            "created: 2026-05-31\n"
            "updated: 2026-05-31\n"
            "source_ids:\n"
            f"  - {source_id}\n"
            f"status: {status}\n"
            f"summary: {title} source note.\n"
            "citation:\n"
            f"  title: {title}\n"
            "  year: 2026\n"
            "---\n\n"
            f"# {title}\n\n"
            "## Citation\n\n"
            f"- Source ID: `{source_id}`\n"
            f"- Normalized record: `{normalized_path}`\n"
            "- Backlink: [[../concepts/contract-concept|Contract Concept]]\n"
        )

    def test_lint_recognizes_source_note_contract_and_lifecycle_states(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))

            results = LINT.run_checks(workspace, LINT.load_config(workspace))
            notes = sorted((workspace / "wiki" / "sources").glob("*.md"))
            note_contract = []
            for note in notes:
                frontmatter, error = LINT.load_frontmatter(note)
                body = note.read_text().split("---", 2)[-1]
                note_contract.append((note.name, frontmatter, error, body))

        categories = {issue["category"] for issue in results["issues"]}
        rows = {row["source_id"]: row for row in results["source_coverage"]}
        self.assertNotIn("source_missing_normalized", categories)
        self.assertNotIn("normalized_missing_source_note", categories)
        self.assertNotIn("integrated_missing_citation", categories)
        self.assertEqual("integrated", rows["paper:contract-paper"]["effective_status"])
        self.assertEqual("noted", rows["link:contract-link"]["effective_status"])
        self.assertEqual("deferred", rows["data:contract-card"]["effective_status"])
        self.assertEqual("rejected", rows["codebase:contract-artifact"]["effective_status"])
        self.assertEqual("superseded", rows["paper:old-contract-paper"]["effective_status"])
        self.assertEqual(
            {
                "discovered": 0,
                "normalized": 0,
                "noted": 1,
                "integrated": 1,
                "deferred": 1,
                "rejected": 1,
                "superseded": 1,
            },
            results["stats"]["source_lifecycle_counts"],
        )
        for name, frontmatter, error, body in note_contract:
            with self.subTest(note=name):
                self.assertIsNone(error)
                self.assertEqual("source", frontmatter["type"])
                self.assertTrue(frontmatter["source_ids"])
                self.assertIn("citation", frontmatter)
                self.assertIn("Source ID:", body)
                self.assertIn("Normalized record:", body)
                self.assertIn("Backlink:", body)

    def build_codebase_workspace(self, root: Path) -> Path:
        workspace = root / "codebase-workspace"
        for relative in (
            "raw/code",
            "sources/normalized",
            "sources/code_wikis",
            "wiki/sources",
            "wiki/claims",
            "wiki/synthesis",
            "wiki/decisions",
        ):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        (workspace / "research.yml").write_text(
            "project:\n"
            "  name: bounded-codebase-intake\n"
            "  description: Synthetic external-worker artifact intake.\n"
            "raw:\n"
            "  source_roots:\n"
            "    - raw/code\n"
            "sources:\n"
            "  manifest_path: sources/manifest.jsonl\n"
            "  normalized_dir: sources/normalized\n"
            "  default_status: integrated\n"
            "  lifecycle_statuses: [discovered, normalized, noted, integrated, deferred, rejected, superseded]\n"
            "wiki:\n"
            "  root: wiki\n"
            "  required_dirs: []\n"
            "  allowed_page_types: [source, claim, synthesis, decision]\n"
            "lint:\n"
            "  validate_structure: false\n"
            "  validate_frontmatter: false\n"
            "  validate_links: false\n"
            "  validate_source_coverage: true\n"
            "  validate_provenance: false\n"
            "  validate_curation_metadata: false\n"
            "  validate_source_requests: false\n"
            "  validate_output_license_status: false\n"
            "  validate_academic_publication_metadata: false\n"
            "  detect_prompt_injection_patterns: false\n"
            "  validate_claims: false\n"
            "  validate_questions: false\n"
            "integrations:\n"
            "  codebase_analysis:\n"
            "    enabled: true\n"
            "    provider: external-artifact\n"
            "    command: null\n"
            "    output_dir: sources/code_wikis\n"
            "    read_only: true\n"
            "    install_hooks: false\n"
            "    background_sync: false\n"
            "    untrusted_input: acknowledged\n",
            encoding="utf-8",
        )
        shutil.copyfile(
            CODEBASE_FIXTURES / "fixture-snapshot.zip",
            workspace / "raw" / "code" / "fixture-snapshot.zip",
        )
        return workspace

    def write_codebase_artifact_manifest(self, artifact_dir: Path, source_id: str) -> None:
        context_path = artifact_dir / "context.json"
        shutil.copyfile(CODEBASE_FIXTURES / "context.json", context_path)
        manifest_path = CODEBASE_FIXTURES / "artifact-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(source_id, manifest["source_id"])
        shutil.copyfile(
            manifest_path,
            artifact_dir / "artifact-manifest.json",
        )

    def write_codebase_maintained_pages(self, workspace: Path, source_id: str, normalized_path: Path) -> list[Path]:
        safe_id = NORMALIZE.safe_source_id(source_id)
        note = workspace / "wiki" / "sources" / f"{safe_id}.md"
        maintained = [
            workspace / "wiki" / "claims" / "bounded-intake-claim.md",
            workspace / "wiki" / "synthesis" / "bounded-intake-synthesis.md",
            workspace / "wiki" / "decisions" / "bounded-intake-decision.md",
        ]
        note.write_text(
            "---\n"
            "type: source\n"
            "status: integrated\n"
            "source_ids:\n"
            f"  - {source_id}\n"
            "---\n\n"
            "# Bounded Codebase Source\n\n"
            f"- [Normalized record](../../{normalized_path.relative_to(workspace).as_posix()})\n"
            "- [[../claims/bounded-intake-claim|Claim backlink]]\n"
            "- [[../synthesis/bounded-intake-synthesis|Synthesis backlink]]\n"
            "- [[../decisions/bounded-intake-decision|Decision backlink]]\n",
            encoding="utf-8",
        )
        page_types = {"claims": "claim", "synthesis": "synthesis", "decisions": "decision"}
        for path in maintained:
            page_type = page_types[path.parent.name]
            path.write_text(
                "---\n"
                f"type: {page_type}\n"
                "source_ids:\n"
                f"  - {source_id}\n"
                "---\n\n"
                f"# Maintained {page_type.title()}\n\n"
                f"Evidence is interpreted through [[../sources/{safe_id}|the maintained source note]].\n",
                encoding="utf-8",
            )
        return [note, *maintained]

    def test_bounded_codebase_artifact_flows_to_bidirectional_maintained_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_codebase_workspace(Path(tmpdir))
            config = INVENTORY.load_config(workspace)
            records, warnings, _summary = INVENTORY.build_records(workspace, config, {})
            codebase_records = [record for record in records if record.get("kind") == "codebase_architecture"]
            self.assertEqual([], [warning for warning in warnings if "codebase" in warning.lower()])
            self.assertEqual(1, len(codebase_records))
            record = codebase_records[0]
            source_id = record["id"]
            self.assertTrue(record["metadata"]["codebase_intake"]["bounded"])
            self.assertEqual("none", record["metadata"]["codebase_intake"]["product_execution"])
            self.assertRegex(record["metadata"]["sha256"], r"^sha256:[0-9a-f]{64}$")
            (workspace / "sources" / "manifest.jsonl").write_text(
                json.dumps(record, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            artifact_dir = workspace / record["metadata"]["codebase_output_dir"]
            artifact_dir.mkdir(parents=True)
            self.write_codebase_artifact_manifest(artifact_dir, source_id)

            with mock.patch.object(
                NORMALIZE.subprocess,
                "run",
                side_effect=AssertionError("codebase intake must not execute subprocesses"),
            ):
                normalized = NORMALIZE.normalize_codebase_record(workspace, config, record)
            normalized_path, action = NORMALIZE.write_normalized_source(
                normalized,
                workspace / "sources" / "normalized",
                "sources/manifest.jsonl",
                "2026-07-11",
                manifest_records=[record],
                project_root=workspace,
                normalized_at="2026-07-11T00:00:00Z",
            )
            frontmatter = NORMALIZE.read_output_frontmatter(normalized_path)

            self.assertEqual("created", action)
            self.assertEqual("codebase_context", normalized.extraction_method)
            self.assertEqual("validated", frontmatter["codebase_intake_status"])
            self.assertEqual("external_worker_only", frontmatter["codebase_execution_scope"])
            self.assertEqual("self_asserted_external_worker", frontmatter["codebase_artifact_provenance"]["trust"])
            self.assertNotEqual(
                "attacker/fake-identity-must-not-be-promoted",
                frontmatter.get("codebase_repo"),
            )
            maintained = self.write_codebase_maintained_pages(workspace, source_id, normalized_path)

            lint_results = LINT.run_checks(workspace, LINT.load_config(workspace))
            categories = {item["category"] for item in lint_results["issues"]}
            self.assertNotIn("codebase_artifact_provenance", categories)
            self.assertNotIn("codebase_evidence_link_missing", categories)
            self.assertEqual(1, lint_results["stats"]["codebase_evidence_records_checked"])
            self.assertFalse(lint_results["stats"]["codebase_product_execution"])

            query_config = QUERY.load_config(workspace)
            documents = QUERY.build_index(workspace, query_config, "all")
            ranked = QUERY.rank_documents(documents, "bounded inert snapshot", 20)
            enriched = QUERY.enrich_evidence_links(ranked, QUERY.evidence_path_graph(workspace, query_config))
            codebase_results = [item for item in enriched if source_id in item["source_ids"]]
            self.assertTrue(codebase_results)
            expected_maintained = sorted(path.relative_to(workspace).as_posix() for path in maintained)
            for result in codebase_results:
                self.assertEqual(
                    [normalized_path.relative_to(workspace).as_posix()],
                    result["evidence_links"]["normalized_paths"],
                )
                self.assertEqual(expected_maintained, result["evidence_links"]["maintained_paths"])

    def test_codebase_product_scope_has_no_adapter_execution_path(self):
        normalize_source = inspect.getsource(NORMALIZE.normalize_codebase_record)
        inventory_source = inspect.getsource(INVENTORY.build_records)
        docs = (REPO_ROOT / "workspace-template" / "docs" / "codebase-analysis.md").read_text()

        self.assertNotIn("subprocess", normalize_source)
        self.assertNotIn("codebase_command_text", normalize_source)
        self.assertNotIn("git clone", inventory_source.lower())
        self.assertNotIn("unpack_archive", inventory_source)
        self.assertIn("No shipped product path interprets or executes it", docs)
        self.assertIn("EvidenceWiki neither clones repositories nor launches", docs)
        self.assertIn("expand the product's trust boundary", docs)
        self.assertIn("cross-platform review", docs)


if __name__ == "__main__":
    unittest.main()
