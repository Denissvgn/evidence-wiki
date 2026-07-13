import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
VERIFY_PATH = SCRIPTS / "verify_citations.py"
ACADEMIC_REPLAY_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "academic-replay"
OPENALEX_PROVIDER_CONTRACTS = json.loads(
    (ACADEMIC_REPLAY_FIXTURES / "openalex-provider-contracts.json").read_text(encoding="utf-8")
)


ATOM_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <published>2026-01-01T00:00:00Z</published>
    <updated>2026-01-02T00:00:00Z</updated>
    <title> Synthetic Retrieval Paper </title>
    <author><name>Ada Lovelace</name></author>
    <author><name>Grace Hopper</name></author>
  </entry>
</feed>
"""


EMPTY_ATOM_RESPONSE = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
</feed>
"""


OPENALEX_WORK = {
    "id": "https://openalex.org/W260100001",
    "doi": "https://doi.org/10.5555/openalex",
    "display_name": "Synthetic Retrieval Paper",
    "publication_year": 2026,
    "authorships": [
        {"author": {"display_name": "Ada Lovelace"}},
        {"author": {"display_name": "Grace Hopper"}},
    ],
}


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"Missing script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_frontmatter(path: Path, frontmatter: dict, body: str = "# Source\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body,
        encoding="utf-8",
    )


class VerifyCitationsTests(unittest.TestCase):
    def setUp(self):
        self.verify = load_script_module("research_verify_citations", VERIFY_PATH)

    def tearDown(self):
        fetch_sources = getattr(self.verify, "fetch_sources", None)
        if fetch_sources is not None:
            fetch_sources.ARXIV_TRANSPORT = None
            fetch_sources.OPENALEX_TRANSPORT = None
            if hasattr(fetch_sources, "DOI_TRANSPORT"):
                fetch_sources.DOI_TRANSPORT = None
            fetch_sources.ARXIV_LAST_REQUEST_AT = None
            fetch_sources.OPENALEX_LAST_REQUEST_AT = None

    def build_workspace(self, root: Path, *, acquisition: dict | None = None) -> Path:
        workspace = root / "workspace"
        (workspace / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        (workspace / "raw" / "papers").mkdir(parents=True, exist_ok=True)
        config = {
            "project": {"name": "verify-citations-test"},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
            },
            "integrations": {
                "acquisition": acquisition
                if acquisition is not None
                else {
                    "enabled": False,
                    "providers": [],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                }
            },
        }
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return workspace

    def add_source(
        self,
        workspace: Path,
        source_id: str,
        normalized: dict,
        *,
        provenance: dict | None = None,
        manifest_metadata: dict | None = None,
    ) -> None:
        manifest = workspace / "sources" / "manifest.jsonl"
        record = {
            "id": source_id,
            "kind": "paper",
            "raw_paths": [f"raw/papers/{source_id.replace(':', '--')}.md"],
            "status": "normalized",
            "detected_at": "2026-07-02T00:00:00Z",
        }
        if provenance is not None:
            record["provenance"] = provenance
        if manifest_metadata is not None:
            record["metadata"] = manifest_metadata
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        write_frontmatter(
            workspace / "sources" / "normalized" / f"{source_id.replace(':', '--')}.md",
            {
                "type": "normalized_source",
                "source_id": source_id,
                "source_kind": "paper",
                "status": "content_extracted",
                **normalized,
            },
        )

    def run_verify(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.verify.main(list(args))
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def install_arxiv_transport(self, payload: bytes) -> list[str]:
        calls: list[str] = []

        def transport(url: str, timeout: float) -> bytes:
            calls.append(url)
            return payload

        self.verify.fetch_sources.ARXIV_TRANSPORT = transport
        self.verify.fetch_sources.ARXIV_CLOCK = lambda: 0.0
        self.verify.fetch_sources.ARXIV_SLEEP = lambda _seconds: None
        self.verify.fetch_sources.ARXIV_LAST_REQUEST_AT = None
        return calls

    def install_openalex_transport(self, payload: bytes | BaseException) -> list[str]:
        calls: list[str] = []

        def transport(url: str, timeout: float, headers: dict[str, str]) -> bytes:
            calls.append(url)
            if isinstance(payload, BaseException):
                raise payload
            return payload

        self.verify.fetch_sources.OPENALEX_TRANSPORT = transport
        self.verify.fetch_sources.OPENALEX_CLOCK = lambda: 0.0
        self.verify.fetch_sources.OPENALEX_SLEEP = lambda _seconds: None
        self.verify.fetch_sources.OPENALEX_LAST_REQUEST_AT = None
        return calls

    def result_by_source(self, document: dict) -> dict[str, dict]:
        return {record["source_id"]: record for record in document["results"]}

    def test_local_provider_provenance_verifies_without_network(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_source(
                workspace,
                "paper:2601.00001v1",
                {
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )

            code, stdout, stderr = self.run_verify("--project-root", str(workspace), "--format", "json")

        self.assertEqual(0, code, stderr)
        document = json.loads(stdout)
        self.assertEqual("verified", document["overall_result"])
        self.assertFalse(document["network_io_executed"])
        result = self.result_by_source(document)["paper:2601.00001v1"]
        self.assertEqual("verified", result["result"])
        self.assertIn("arxiv_id", result["identifiers"])
        self.assertEqual("citation_identity_quorum", result["policy"])
        self.assertEqual(["arxiv_id"], result["comparisons"]["offline_identity"]["matched_keys"])

    def test_local_identity_normalization_rejects_genuine_wrong_work_substitution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_source(
                workspace,
                "paper:normalized-arxiv",
                {
                    "title": "Normalized Identifier Paper",
                    "arxiv_id": "arXiv:2601.00001V1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )
            self.add_source(
                workspace,
                "paper:normalized-openalex",
                {
                    "title": "Normalized OpenAlex Paper",
                    "openalex_id": "https://openalex.org/w260100001",
                    "doi": "HTTPS://DOI.ORG/10.5555/Normalized.Identity",
                },
                provenance={
                    "origin_url": "https://openalex.org/W260100001",
                    "retrieved_by": "fetch_sources.py/openalex",
                    "doi": "doi:10.5555/normalized.identity",
                },
            )
            self.add_source(
                workspace,
                "paper:wrong-work",
                {
                    "title": "Requested Work",
                    "arxiv_id": "2601.00002v1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.99999v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )

            code, stdout, stderr = self.run_verify("--project-root", str(workspace), "--format", "json")

        self.assertEqual(1, code, stderr)
        results = self.result_by_source(json.loads(stdout))
        arxiv = results["paper:normalized-arxiv"]
        openalex = results["paper:normalized-openalex"]
        wrong_work = results["paper:wrong-work"]
        self.assertEqual("verified", arxiv["result"])
        self.assertEqual(["arxiv_id"], arxiv["comparisons"]["offline_identity"]["matched_keys"])
        self.assertEqual("verified", openalex["result"])
        self.assertEqual(
            ["openalex_id", "doi"],
            openalex["comparisons"]["offline_identity"]["matched_keys"],
        )
        self.assertEqual("mismatch", wrong_work["result"])
        conflict = wrong_work["comparisons"]["offline_identity"]["conflicts"][0]
        self.assertEqual("arxiv_id", conflict["identifier"])
        self.assertEqual("2601.00002v1", conflict["local"])
        self.assertEqual("2601.99999v1", conflict["provenance"])
        self.assertIn("reacquire the exact work", wrong_work["remediation"])

    def test_local_valid_metadata_without_provider_provenance_skips_no_live(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_source(
                workspace,
                "paper:doi-only",
                {
                    "title": "Synthetic DOI Paper",
                    "publication_year": 2025,
                    "doi": "10.5555/doi-only",
                },
            )

            code, stdout, stderr = self.run_verify("--project-root", str(workspace), "--format", "json")

        self.assertEqual(1, code, stderr)
        document = json.loads(stdout)
        self.assertEqual("no_ship", document["overall_result"])
        result = self.result_by_source(document)["paper:doi-only"]
        self.assertEqual("skipped_no_live", result["result"])

    def test_requested_source_with_missing_title_or_identifier_is_insufficient(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_source(
                workspace,
                "paper:missing-title",
                {"doi": "10.5555/missing-title"},
            )

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--source-id",
                "paper:missing-title",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:missing-title"]
        self.assertEqual("insufficient_metadata", result["result"])
        self.assertTrue(any("title" in reason.lower() for reason in result["reasons"]))

    def test_live_arxiv_verifies_mismatches_and_not_found_with_mocked_transport(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:2601.00001v1",
                {
                    "title": "Synthetic Retrieval Paper",
                    "title_source": "provider",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )
            calls = self.install_arxiv_transport(ATOM_RESPONSE)

            code, stdout, stderr = self.run_verify(
                "--project-root", str(workspace), "--format", "json", "--live", "--provider", "arxiv"
            )

            self.assertEqual(0, code, stderr)
            result = self.result_by_source(json.loads(stdout))["paper:2601.00001v1"]
            self.assertEqual("verified", result["result"])
            self.assertEqual("provider", result["title_source"])
            self.assertTrue(calls)

            self.add_source(
                workspace,
                "paper:2601.00002v1",
                {
                    "title": "Different Local Title",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
            )
            self.install_arxiv_transport(ATOM_RESPONSE)

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "arxiv",
                "--source-id",
                "paper:2601.00002v1",
            )

            self.assertEqual(1, code, stderr)
            result = self.result_by_source(json.loads(stdout))["paper:2601.00002v1"]
            self.assertEqual("mismatch", result["result"])

            self.add_source(
                workspace,
                "paper:2601.00003v1",
                {
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Katherine Johnson"],
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )
            self.install_arxiv_transport(ATOM_RESPONSE)

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "arxiv",
                "--source-id",
                "paper:2601.00003v1",
            )

            self.assertEqual(1, code, stderr)
            result = self.result_by_source(json.loads(stdout))["paper:2601.00003v1"]
            self.assertEqual("mismatch", result["result"])
            self.assertTrue(any("author" in reason.lower() for reason in result["reasons"]))

            self.install_arxiv_transport(EMPTY_ATOM_RESPONSE)
            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "arxiv",
                "--source-id",
                "paper:2601.00001v1",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:2601.00001v1"]
        self.assertEqual("not_found", result["result"])

    def test_live_provider_backed_source_with_empty_authors_is_insufficient_metadata(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:2601.00001v1",
                {
                    "title": "Synthetic Retrieval Paper",
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2601.00001v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                },
            )

            def transport(_url: str, _timeout: float) -> bytes:
                raise AssertionError("empty provider-backed authors must fail before live fetch")

            self.verify.fetch_sources.ARXIV_TRANSPORT = transport
            self.verify.fetch_sources.ARXIV_CLOCK = lambda: 0.0
            self.verify.fetch_sources.ARXIV_SLEEP = lambda _seconds: None
            self.verify.fetch_sources.ARXIV_LAST_REQUEST_AT = None

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "arxiv",
                "--source-id",
                "paper:2601.00001v1",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:2601.00001v1"]
        self.assertEqual("insufficient_metadata", result["result"])
        self.assertTrue(any("author" in reason.lower() for reason in result["reasons"]))
        self.assertTrue(any("openalex enrich" in reason.lower() for reason in result["reasons"]))

    def test_live_openalex_verifies_doi_work_id_mismatch_and_not_found(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:openalex",
                {
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "doi": "10.5555/openalex",
                    "openalex_id": "W260100001",
                },
            )
            self.install_openalex_transport(json.dumps(OPENALEX_WORK).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root", str(workspace), "--format", "json", "--live", "--provider", "openalex"
            )

            self.assertEqual(0, code, stderr)
            result = self.result_by_source(json.loads(stdout))["paper:openalex"]
            self.assertEqual("verified", result["result"])

            self.add_source(
                workspace,
                "paper:openalex-mismatch",
                {
                    "title": "Different Local Title",
                    "authors": ["Ada Lovelace"],
                    "publication_year": 2026,
                    "openalex_id": "W260100001",
                },
            )
            self.install_openalex_transport(json.dumps(OPENALEX_WORK).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
                "--source-id",
                "paper:openalex-mismatch",
            )

            self.assertEqual(1, code, stderr)
            result = self.result_by_source(json.loads(stdout))["paper:openalex-mismatch"]
            self.assertEqual("mismatch", result["result"])

            self.install_openalex_transport(
                HTTPError("https://api.openalex.org/works/W260100001?api_key=SECRET", 404, "not found", None, None)
            )
            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
                "--source-id",
                "paper:openalex",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:openalex"]
        self.assertEqual("not_found", result["result"])
        self.assertNotIn("SECRET", stdout)
        self.assertNotIn("api_key=", stdout)

    def test_live_openalex_missing_requested_doi_is_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:openalex-missing-doi",
                {
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Ada Lovelace", "Grace Hopper"],
                    "publication_year": 2026,
                    "doi": "10.5555/openalex",
                    "openalex_id": "W260100001",
                },
            )
            provider_work = OPENALEX_PROVIDER_CONTRACTS["missing_doi_work"]
            self.install_openalex_transport(json.dumps(provider_work).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:openalex-missing-doi"]
        self.assertEqual("mismatch", result["result"])
        self.assertEqual("10.5555/openalex", result["comparisons"]["doi"]["local"])
        self.assertIsNone(result["comparisons"]["doi"]["provider"])
        self.assertFalse(result["comparisons"]["doi"]["matched"])
        self.assertTrue(any("DOI mismatch" in reason for reason in result["reasons"]))

    def test_live_arxiv_requested_version_conflict_is_mismatch_not_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:2601.00001v1",
                {
                    "title": "Synthetic Retrieval Paper",
                    "authors": ["Ada Lovelace", "Grace Hopper"],
                    "publication_year": 2026,
                    "arxiv_id": "2601.00001v1",
                },
            )
            payload = (ACADEMIC_REPLAY_FIXTURES / "arxiv-version-conflict-feed.xml").read_bytes()
            self.install_arxiv_transport(payload)

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "arxiv",
            )

        self.assertEqual(1, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:2601.00001v1"]
        self.assertEqual("mismatch", result["result"])
        self.assertTrue(result["comparisons"]["arxiv_id"]["version_conflict"])
        self.assertTrue(any("version mismatch" in reason for reason in result["reasons"]))

    def test_author_name_forms_match_live_openalex_variants_and_preserve_negatives(self):
        matching_pairs = [
            (["john miller"], ["j. j. miller"]),
            (["mickael seznec", "hao wu"], ["mickaël seznec", "wu, hao"]),
            (
                ["shang yang", "wei-ming chen", "wei-chen wang", "guangxuan xiao", "xingyu dang", "chuang gan", "song han"],
                ["yang shang", "chen, wei-ming", "wang, wei-chen", "xiao, guangxuan", "dang, xingyu", "gan, chuang", "han, song"],
            ),
            (["sanmi koyejo"], ["oluwasanmi koyejo"]),
        ]
        for local, provider in matching_pairs:
            with self.subTest(local=local, provider=provider):
                comparison = self.verify.author_sets_match(local, provider)
                self.assertTrue(comparison["matched"], comparison)
                self.assertEqual(len(local), len(comparison["matches"]))

        negative_pairs = [
            (["amir zandieh", "majid daliri"], ["alice example", "bob example"]),
            (["john miller"], ["alice miller"]),
            (["li chen"], ["lima chen"]),
            (["wei chen", "wei chen"], ["wei chen"]),
        ]
        for local, provider in negative_pairs:
            with self.subTest(local=local, provider=provider):
                comparison = self.verify.author_sets_match(local, provider)
                self.assertFalse(comparison["matched"], comparison)
                self.assertTrue(comparison["unmatched_local"], comparison)

    def test_live_openalex_author_name_forms_return_auditable_match_details(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:name-forms",
                {
                    "title": "Name Form Paper",
                    "authors": ["Mickael Seznec", "Hao Wu", "Sanmi Koyejo"],
                    "publication_year": 2024,
                    "doi": "10.5555/name-forms",
                    "openalex_id": "W5555",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2407.04620v4",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                },
            )
            work = {
                "id": "https://openalex.org/W5555",
                "doi": "https://doi.org/10.5555/name-forms",
                "display_name": "Name Form Paper",
                "publication_year": 2024,
                "authorships": [
                    {"author": {"display_name": "Mickaël Seznec"}},
                    {"author": {"display_name": "Wu, Hao"}},
                    {"author": {"display_name": "Oluwasanmi Koyejo"}},
                ],
            }
            self.install_openalex_transport(json.dumps(work).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
            )

        self.assertEqual(0, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:name-forms"]
        self.assertEqual("verified", result["result"])
        self.assertTrue(result["comparisons"]["authors"]["matched"])
        self.assertEqual(
            [
                {"local": "mickael seznec", "provider": "mickael seznec", "rule": "canonical_tokens"},
                {"local": "hao wu", "provider": "hao wu", "rule": "canonical_tokens"},
                {"local": "sanmi koyejo", "provider": "oluwasanmi koyejo", "rule": "family_given_compatible"},
            ],
            result["comparisons"]["authors"]["matches"],
        )

    def test_live_openalex_version_lag_is_verified_with_diagnostic_for_provider_acquired_source(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:2402.02750v2",
                {
                    "title": "KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache",
                    "authors": ["Yuhang Li", "Mingzhe Wang"],
                    "publication_year": 2024,
                    "arxiv_id": "2402.02750v2",
                    "doi": "10.48550/arxiv.2402.02750",
                    "openalex_id": "W424202750",
                },
                provenance={
                    "origin_url": "https://arxiv.org/abs/2402.02750v2",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                },
            )
            work = {
                "id": "https://openalex.org/W424202750",
                "doi": "https://doi.org/10.48550/arXiv.2402.02750",
                "display_name": "KIVI : Plug-and-play 2bit KV Cache Quantization with Streaming Asymmetric Quantization",
                "publication_year": 2023,
                "authorships": [
                    {"author": {"display_name": "Yuhang Li"}},
                    {"author": {"display_name": "Mingzhe Wang"}},
                ],
            }
            self.install_openalex_transport(json.dumps(work).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
            )

        self.assertEqual(0, code, stderr)
        result = self.result_by_source(json.loads(stdout))["paper:2402.02750v2"]
        self.assertEqual("verified", result["result"])
        self.assertTrue(any("openalex_title_version_lag" in reason for reason in result["reasons"]))
        self.assertTrue(result["comparisons"]["title"]["lag_suspected"])
        self.assertTrue(result["comparisons"]["year"]["lag_suspected"])

    def test_live_openalex_wrong_work_requires_recorded_quorum(self):
        wrong_work = {
            "id": "https://openalex.org/W4416915242",
            "doi": "https://doi.org/10.48550/arXiv.2504.19874",
            "display_name": "Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
            "publication_year": 2025,
            "authorships": [
                {"author": {"display_name": "Alex Proof"}},
                {"author": {"display_name": "Taylor Logic"}},
            ],
        }
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            source = {
                "title": "TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                "authors": ["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                "publication_year": 2025,
                "arxiv_id": "2504.19874v1",
                "doi": "10.48550/arxiv.2504.19874",
                "openalex_id": "W4416915242",
            }
            self.add_source(
                workspace,
                "paper:2504.19874v1",
                source,
                provenance={
                    "origin_url": "https://arxiv.org/abs/2504.19874v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                    "openalex_identity_conflict": True,
                    "doi_resolution": {
                        "status": "resolved",
                        "resolved_url": "https://arxiv.org/abs/2504.19874",
                        "matches_arxiv_id": True,
                    },
                },
            )
            self.add_source(
                workspace,
                "paper:2504.19874v1-unrecorded",
                source,
                provenance={
                    "origin_url": "https://arxiv.org/abs/2504.19874v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                },
            )
            self.add_source(
                workspace,
                "paper:2504.19874v1-uncorroborated",
                source,
                provenance={
                    "origin_url": "https://arxiv.org/abs/2504.19874v1",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "academic_provider": "arxiv",
                    "openalex_identity_conflict": True,
                    "doi_resolution": {
                        "status": "redirect_mismatch",
                        "resolved_url": "https://example.org/not-the-paper",
                        "matches_arxiv_id": False,
                    },
                },
            )

            self.install_openalex_transport(json.dumps(wrong_work).encode("utf-8"))
            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
                "--source-id",
                "paper:2504.19874v1",
            )
            self.assertEqual(0, code, stderr)
            recorded = self.result_by_source(json.loads(stdout))["paper:2504.19874v1"]

            self.install_openalex_transport(json.dumps(wrong_work).encode("utf-8"))
            uncorroborated_code, uncorroborated_stdout, uncorroborated_stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
                "--source-id",
                "paper:2504.19874v1-uncorroborated",
            )

            self.install_openalex_transport(json.dumps(wrong_work).encode("utf-8"))
            code, stdout, stderr = self.run_verify(
                "--project-root",
                str(workspace),
                "--format",
                "json",
                "--live",
                "--provider",
                "openalex",
                "--source-id",
                "paper:2504.19874v1-unrecorded",
            )

        self.assertEqual("verified", recorded["result"])
        self.assertTrue(any("openalex_identity_conflict_recorded" in reason for reason in recorded["reasons"]))
        self.assertTrue(any("openalex_identity_quorum_verified" in reason for reason in recorded["reasons"]))
        self.assertEqual(1, uncorroborated_code, uncorroborated_stderr)
        uncorroborated = self.result_by_source(json.loads(uncorroborated_stdout))["paper:2504.19874v1-uncorroborated"]
        self.assertEqual("mismatch", uncorroborated["result"])
        self.assertTrue(
            any("openalex_identity_conflict_uncorroborated" in reason for reason in uncorroborated["reasons"])
        )
        self.assertEqual(1, code, stderr)
        unrecorded = self.result_by_source(json.loads(stdout))["paper:2504.19874v1-unrecorded"]
        self.assertEqual("mismatch", unrecorded["result"])
        self.assertTrue(any("openalex_identity_conflict_unrecorded" in reason for reason in unrecorded["reasons"]))

    def test_live_mode_refuses_disabled_or_disallowed_provider_before_transport(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            self.add_source(
                workspace,
                "paper:2601.00001v1",
                {"title": "Synthetic Retrieval Paper", "arxiv_id": "2601.00001v1"},
            )
            calls = self.install_arxiv_transport(ATOM_RESPONSE)

            code, stdout, stderr = self.run_verify(
                "--project-root", str(workspace), "--format", "json", "--live", "--provider", "arxiv"
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], calls)
        self.assertEqual("ACQUISITION_DISABLED", json.loads(stderr)["error_code"])

        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["arxiv"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:openalex",
                {"title": "Synthetic Retrieval Paper", "openalex_id": "W260100001"},
            )
            calls = self.install_openalex_transport(json.dumps(OPENALEX_WORK).encode("utf-8"))

            code, stdout, stderr = self.run_verify(
                "--project-root", str(workspace), "--format", "json", "--live", "--provider", "openalex"
            )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual([], calls)
        self.assertEqual("ACQUISITION_PROVIDER_DISABLED", json.loads(stderr)["error_code"])

    def test_openalex_api_key_is_redacted_from_fatal_and_result_output(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:openalex",
                {"title": "Synthetic Retrieval Paper", "openalex_id": "W260100001"},
            )
            self.install_openalex_transport(
                HTTPError("https://api.openalex.org/works/W260100001?api_key=secret-value", 404, "not found", None, None)
            )
            with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret-value"}):
                code, stdout, stderr = self.run_verify(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "--live",
                    "--provider",
                    "openalex",
                )

        self.assertEqual(1, code, stderr)
        self.assertNotIn("secret-value", stdout)
        self.assertNotIn("api_key=", stdout)

        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(
                Path(tmpdir),
                acquisition={
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 10,
                    "require_license_check": True,
                },
            )
            self.add_source(
                workspace,
                "paper:openalex",
                {"title": "Synthetic Retrieval Paper", "openalex_id": "W260100001"},
            )
            self.install_openalex_transport(URLError("connection failed with api_key=secret-value"))
            with mock.patch.dict(os.environ, {"OPENALEX_API_KEY": "secret-value"}):
                code, stdout, stderr = self.run_verify(
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "--live",
                    "--provider",
                    "openalex",
                )

        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertNotIn("secret-value", stderr)
        self.assertNotIn("api_key=", stderr)


if __name__ == "__main__":
    unittest.main()
