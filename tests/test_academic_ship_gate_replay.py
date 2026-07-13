import argparse
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
from urllib.parse import parse_qs, unquote, urlparse
from xml.sax.saxutils import escape

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "academic-replay"
PAPER_IDS = [f"2601.{index:05d}v1" for index in range(1, 12)]
TITLE = "Synthetic Retrieval Paper"
AUTHORS = ["Ada Lovelace", "Grace Hopper"]
DOI = "10.5555/synthetic-retrieval-paper"
OPENALEX_ID = "W260100001"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VERIFY = load_script_module("academic_replay_verify", "verify_citations.py")
INVENTORY = load_script_module("academic_replay_inventory", "source_inventory.py")
NORMALIZE = load_script_module("academic_replay_normalize", "normalize_sources.py")
LINT = load_script_module("academic_replay_lint", "lint.py")
READINESS = load_script_module("academic_replay_readiness", "publication_readiness.py")


def write_frontmatter(path: Path, frontmatter: dict[str, Any], body: str = "# Source\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body,
        encoding="utf-8",
    )


class AcademicShipGateReplayTests(unittest.TestCase):
    def tearDown(self):
        VERIFY.fetch_sources.ARXIV_TRANSPORT = None
        VERIFY.fetch_sources.OPENALEX_TRANSPORT = None
        if hasattr(VERIFY.fetch_sources, "DOI_TRANSPORT"):
            VERIFY.fetch_sources.DOI_TRANSPORT = None
        VERIFY.fetch_sources.ARXIV_LAST_REQUEST_AT = None
        VERIFY.fetch_sources.OPENALEX_LAST_REQUEST_AT = None

    def write_health_complete_config(self, workspace: Path, config: dict[str, Any]) -> None:
        """Keep replay workspaces valid under the same fail-closed health gate as real workspaces."""
        for relative in ("scripts", "docs", "skills", "raw", "sources", "wiki"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        for relative, content in (
            ("workspace-system.yml", "schema_version: '1.0'\n"),
            ("AGENTS.md", "# Academic replay agent boundary\n"),
            ("index.md", "# Academic replay workspace\n"),
            ("log.md", "# Research log\n"),
        ):
            (workspace / relative).write_text(content, encoding="utf-8")
        for section in ("taxonomy", "ingest", "outputs"):
            config.setdefault(section, {})
        (workspace / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def install_arxiv_records(self, records: dict[str, dict[str, Any]]) -> None:
        normalized_records = {key.casefold(): value for key, value in records.items()}

        def entry(arxiv_id: str, record: dict[str, Any]) -> str:
            authors = "\n".join(f"<author><name>{escape(author)}</name></author>" for author in record["authors"])
            return (
                "<entry>"
                f"<id>http://arxiv.org/abs/{escape(arxiv_id)}</id>"
                f"<published>{record['year']}-01-01T00:00:00Z</published>"
                f"<updated>{record['year']}-01-02T00:00:00Z</updated>"
                f"<title>{escape(record['title'])}</title>"
                f"{authors}"
                "</entry>"
            )

        def transport(url: str, _timeout: float) -> bytes:
            query = parse_qs(urlparse(url).query)
            requested = query.get("id_list", [""])[0].split(",")
            entries = [
                entry(arxiv_id, normalized_records[arxiv_id.casefold()])
                for arxiv_id in requested
                if arxiv_id.casefold() in normalized_records
            ]
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">'
                + "".join(entries)
                + "</feed>"
            ).encode("utf-8")

        VERIFY.fetch_sources.ARXIV_TRANSPORT = transport
        VERIFY.fetch_sources.ARXIV_CLOCK = lambda: 0.0
        VERIFY.fetch_sources.ARXIV_SLEEP = lambda _seconds: None
        VERIFY.fetch_sources.ARXIV_LAST_REQUEST_AT = None

    def install_provider_fixtures(self) -> None:
        arxiv_payload = (FIXTURES / "arxiv-feed.xml").read_bytes()
        openalex_payload = (FIXTURES / "openalex-work.json").read_bytes()

        VERIFY.fetch_sources.ARXIV_TRANSPORT = lambda _url, _timeout: arxiv_payload
        VERIFY.fetch_sources.OPENALEX_TRANSPORT = lambda _url, _timeout, _headers: openalex_payload
        VERIFY.fetch_sources.ARXIV_CLOCK = lambda: 0.0
        VERIFY.fetch_sources.OPENALEX_CLOCK = lambda: 0.0
        VERIFY.fetch_sources.ARXIV_SLEEP = lambda _seconds: None
        VERIFY.fetch_sources.OPENALEX_SLEEP = lambda _seconds: None
        VERIFY.fetch_sources.ARXIV_LAST_REQUEST_AT = None
        VERIFY.fetch_sources.OPENALEX_LAST_REQUEST_AT = None

    def install_openalex_records(self, records: dict[str, dict[str, Any]]) -> None:
        encoded_records = {
            key.casefold(): json.dumps(value, sort_keys=True).encode("utf-8")
            for key, value in records.items()
        }

        def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
            path = unquote(urlparse(url).path)
            identifier = path.rsplit("/", 1)[-1].casefold()
            payload = encoded_records.get(identifier)
            if payload is None:
                raise AssertionError(f"Unexpected OpenAlex replay request: {url}")
            return payload

        VERIFY.fetch_sources.OPENALEX_TRANSPORT = transport
        VERIFY.fetch_sources.OPENALEX_CLOCK = lambda: 0.0
        VERIFY.fetch_sources.OPENALEX_SLEEP = lambda _seconds: None
        VERIFY.fetch_sources.OPENALEX_LAST_REQUEST_AT = None

    def install_single_openalex_work(self, work: dict[str, Any], *, expected_identifier: str) -> None:
        payload = json.dumps(work, sort_keys=True).encode("utf-8")
        normalized_expected = expected_identifier.casefold()

        def transport(url: str, _timeout: float, _headers: dict[str, str]) -> bytes:
            path = unquote(urlparse(url).path)
            identifier = path.removeprefix("/works/").casefold()
            if identifier != normalized_expected:
                raise AssertionError(f"Unexpected OpenAlex replay request: {url}")
            return payload

        VERIFY.fetch_sources.OPENALEX_TRANSPORT = transport
        VERIFY.fetch_sources.OPENALEX_CLOCK = lambda: 0.0
        VERIFY.fetch_sources.OPENALEX_SLEEP = lambda _seconds: None
        VERIFY.fetch_sources.OPENALEX_LAST_REQUEST_AT = None

    def install_doi_resolution(self, final_url: str) -> None:
        def transport(_url: str, _timeout: float, _headers: dict[str, str]):
            return VERIFY.fetch_sources.result_from_bytes(
                b"",
                url=final_url,
                http_status=200,
                redirect_chain=[final_url],
            )

        VERIFY.fetch_sources.DOI_TRANSPORT = transport

    def openalex_work(
        self,
        *,
        openalex_id: str,
        doi: str,
        title: str,
        authors: list[str],
        year: int,
    ) -> dict[str, Any]:
        return {
            "id": f"https://openalex.org/{openalex_id}",
            "doi": f"https://doi.org/{doi}",
            "display_name": title,
            "publication_year": year,
            "type": "article",
            "authorships": [{"author": {"display_name": author}} for author in authors],
            "open_access": {"is_oa": True, "oa_status": "green"},
        }

    def build_workspace(self, root: Path, *, broken: bool = False) -> Path:
        workspace = root / ("broken-workspace" if broken else "ship-workspace")
        for relative in (
            "raw/papers",
            "raw/web",
            "sources/normalized",
            "sources/discovery",
            "wiki/questions",
            "wiki/synthesis",
        ):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        config = {
            "project": {"name": "academic-replay"},
            "raw": {"source_roots": ["raw/papers", "raw/web"]},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
                "source_requests_path": "sources/source-requests.jsonl",
                "coverage_dir": "sources/coverage",
            },
            "wiki": {"root": "wiki"},
            "integrations": {
                "acquisition": {
                    "enabled": True,
                    "providers": ["arxiv", "openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 20,
                },
                "discovery": {"enabled": True},
            },
            "lint": {
                "validate_structure": False,
                "validate_frontmatter": False,
                "validate_links": False,
                "validate_source_coverage": False,
                "validate_claims": False,
                "validate_questions": False,
                "validate_source_requests": False,
                "validate_curation_metadata": False,
                "validate_output_license_status": False,
                "detect_prompt_injection_patterns": False,
                "validate_provenance": True,
                "validate_academic_publication_metadata": True,
            },
        }
        self.write_health_complete_config(workspace, config)
        records = []
        for index, arxiv_id in enumerate(PAPER_IDS):
            first_broken = broken and index == 0
            source_id = f"paper:{arxiv_id}"
            raw_relative = f"raw/papers/{arxiv_id}.pdf"
            raw_path = workspace / raw_relative
            raw_path.write_bytes(f"PDF fixture for {arxiv_id}\n".encode())
            checksum = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
            provenance = {
                "origin_url": f"https://arxiv.org/abs/{arxiv_id}",
                "retrieved_at": "2026-07-05T00:00:00Z",
                "retrieved_by": "fetch_sources.py/arxiv",
                "license": "CC-BY-4.0",
                "terms_url": f"https://arxiv.org/abs/{arxiv_id}",
                "checksum": checksum,
                "checksum_verified": True,
                "sidecar_path": f"{raw_relative}.provenance.yml",
                "request_id": "req-academic-replay",
                "candidate_id": f"cand-{arxiv_id.replace('.', '-')}",
                "academic_provider": "arxiv",
                "academic_source_type": "preprint",
                "venue": "arXiv",
                "publication_year": 2026,
                "oa_status": "green",
                "peer_review_status": "preprint",
                "title": TITLE,
                "authors": AUTHORS,
                "published": "2026-01-01T00:00:00Z",
                "doi": DOI,
                "doi_source": "openalex",
                "openalex_work_id": OPENALEX_ID,
                "openalex_publication_year": 2026,
            }
            if first_broken:
                provenance.pop("license")
                provenance.pop("doi")
                provenance.pop("openalex_work_id")
            (workspace / f"{raw_relative}.provenance.yml").write_text(
                yaml.safe_dump(provenance, sort_keys=False),
                encoding="utf-8",
            )
            normalized = {
                "type": "normalized_source",
                "source_id": source_id,
                "source_kind": "paper",
                "status": "content_extracted",
                "evidence_usable": True,
                "created": "2026-07-05",
                "updated": "2026-07-05",
                "normalized_at": "2026-07-05T00:00:00Z",
                "raw_paths": [raw_relative],
                "manifest_path": "sources/manifest.jsonl",
                "normalizer": {"name": "normalize_sources.py", "version": 1},
                "parse_warnings": [],
                "title": f"{TITLE} Ada Lovelace Abstract leaked" if first_broken else TITLE,
                "title_source": "pdf_inference" if first_broken else "provider",
                "authors": AUTHORS,
                "publication_year": 2026,
                "arxiv_id": arxiv_id,
                "doi": None if first_broken else DOI,
                "openalex_id": None if first_broken else OPENALEX_ID,
                "provenance": provenance,
            }
            write_frontmatter(workspace / "sources" / "normalized" / f"paper--{arxiv_id}.md", normalized)
            records.append(
                {
                    "id": source_id,
                    "kind": "paper",
                    "status": "normalized",
                    "raw_paths": [raw_relative],
                    "detected_at": "2026-07-05T00:00:00Z",
                    "provenance": provenance,
                }
            )

        self.add_vendor_source(workspace, records, broken=broken)
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        return workspace

    def write_academic_source(
        self,
        workspace: Path,
        records: list[dict[str, Any]],
        *,
        arxiv_id: str,
        openalex_id: str,
        doi: str,
        title: str,
        authors: list[str],
        year: int,
        provenance_extra: dict[str, Any] | None = None,
        normalized_extra: dict[str, Any] | None = None,
    ) -> None:
        source_id = f"paper:{arxiv_id}"
        raw_relative = f"raw/papers/{arxiv_id}.pdf"
        raw_path = workspace / raw_relative
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(f"PDF replay fixture for {arxiv_id}\n".encode())
        checksum = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
        provenance = {
            "origin_url": f"https://arxiv.org/abs/{arxiv_id}",
            "retrieved_at": "2026-07-09T00:00:00Z",
            "retrieved_by": "fetch_sources.py/arxiv",
            "license": "CC-BY-4.0",
            "terms_url": f"https://arxiv.org/abs/{arxiv_id}",
            "checksum": checksum,
            "checksum_verified": True,
            "sidecar_path": f"{raw_relative}.provenance.yml",
            "request_id": "req-live-issue-replay",
            "candidate_id": f"cand-{arxiv_id.replace('.', '-')}",
            "academic_provider": "arxiv",
            "academic_source_type": "preprint",
            "venue": "arXiv",
            "publication_year": year,
            "oa_status": "green",
            "peer_review_status": "preprint",
            "title": title,
            "authors": authors,
            "published": f"{year}-01-01T00:00:00Z",
            "doi": doi,
            "doi_source": "openalex",
            "openalex_work_id": openalex_id,
            "openalex_publication_year": year,
        }
        if provenance_extra:
            provenance.update(provenance_extra)
        (workspace / f"{raw_relative}.provenance.yml").write_text(
            yaml.safe_dump(provenance, sort_keys=False),
            encoding="utf-8",
        )
        normalized = {
            "type": "normalized_source",
            "source_id": source_id,
            "source_kind": "paper",
            "status": "content_extracted",
            "evidence_usable": True,
            "created": "2026-07-09",
            "updated": "2026-07-09",
            "normalized_at": "2026-07-09T00:00:00Z",
            "raw_paths": [raw_relative],
            "raw_pdf": raw_relative,
            "manifest_path": "sources/manifest.jsonl",
            "normalizer": {"name": "normalize_sources.py", "version": 1},
            "parse_warnings": [],
            "title": title,
            "title_source": "provider",
            "authors": authors,
            "publication_year": year,
            "arxiv_id": arxiv_id,
            "doi": doi,
            "openalex_id": openalex_id,
            "provenance": provenance,
        }
        if normalized_extra:
            normalized.update(normalized_extra)
        write_frontmatter(workspace / "sources" / "normalized" / f"paper--{arxiv_id}.md", normalized)
        records.append(
            {
                "id": source_id,
                "kind": "paper",
                "status": "normalized",
                "raw_paths": [raw_relative],
                "detected_at": "2026-07-09T00:00:00Z",
                "provenance": provenance,
            }
        )

    def build_live_issue_workspace(self, root: Path, *, record_conflict: bool = True) -> Path:
        workspace = root / ("live-issue-replay" if record_conflict else "live-issue-unrecorded-conflict")
        for relative in ("raw/papers", "sources/normalized", "sources/discovery", "wiki/questions", "wiki/synthesis"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        config = {
            "project": {"name": "academic-live-issue-replay"},
            "raw": {"source_roots": ["raw/papers"]},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
                "source_requests_path": "sources/source-requests.jsonl",
                "coverage_dir": "sources/coverage",
            },
            "wiki": {"root": "wiki"},
            "integrations": {
                "acquisition": {
                    "enabled": True,
                    "providers": ["arxiv", "openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 20,
                },
                "discovery": {"enabled": True},
            },
            "lint": {
                "validate_structure": False,
                "validate_frontmatter": False,
                "validate_links": False,
                "validate_source_coverage": False,
                "validate_claims": False,
                "validate_questions": False,
                "validate_source_requests": False,
                "validate_curation_metadata": False,
                "validate_output_license_status": False,
                "detect_prompt_injection_patterns": False,
                "validate_provenance": True,
                "validate_academic_publication_metadata": True,
            },
        }
        self.write_health_complete_config(workspace, config)
        records: list[dict[str, Any]] = []
        self.write_academic_source(
            workspace,
            records,
            arxiv_id="2601.10001v1",
            openalex_id="W260110001",
            doi="10.5555/live-name-form",
            title="Author Name Form Replay",
            authors=["John Miller", "Mickael Seznec", "Hao Wu", "Sanmi Koyejo"],
            year=2026,
        )
        self.write_academic_source(
            workspace,
            records,
            arxiv_id="2601.10002v1",
            openalex_id="W260110002",
            doi="10.5555/live-title-lag",
            title="KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache",
            authors=["Zirui Liu", "Jiayi Yuan"],
            year=2026,
            normalized_extra={
                "openalex_title_lag": True,
                "openalex_reported_title": (
                    "KIVI : Plug-and-play 2bit KV Cache Quantization with Streaming Asymmetric Quantization"
                ),
                "openalex_reported_publication_year": 2025,
                "openalex_identity_evidence": {
                    "identifiers_match": True,
                    "canonical_authors_match": True,
                },
            },
        )
        conflict_extra = {
            "openalex_reported_title": "Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
            "openalex_reported_authors": ["Riley Chen", "Morgan Park"],
            "openalex_identity_evidence": {
                "title_matches": False,
                "canonical_authors_match": False,
                "identifiers_match": True,
            },
        }
        if record_conflict:
            conflict_extra.update(
                {
                    "openalex_identity_conflict": True,
                    # This shared replay fixture models an already-enriched
                    # sidecar/normalized state. Dedicated tests below drive the
                    # real openalex enrich path that produces these fields.
                    "doi_resolution": {
                        "doi": "10.5555/live-wrong-work",
                        "arxiv_id": "2601.10003v1",
                        "matches_arxiv_id": True,
                    },
                }
            )
        self.write_academic_source(
            workspace,
            records,
            arxiv_id="2601.10003v1",
            openalex_id="W260110003",
            doi="10.5555/live-wrong-work",
            title="TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
            authors=["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
            year=2026,
            normalized_extra=conflict_extra,
        )
        self.write_academic_source(
            workspace,
            records,
            arxiv_id="2601.10004v1",
            openalex_id="W260110004",
            doi="10.5555/live-public-domain",
            title="Public Domain License Replay",
            authors=["Ada Lovelace"],
            year=2026,
            provenance_extra={
                "license": "CC0-1.0",
                "provider_license_slug": "public-domain",
                "license_source": "openalex.best_oa_location.license",
            },
        )
        self.write_academic_source(
            workspace,
            records,
            arxiv_id="2601.10005v1",
            openalex_id="W260110005",
            doi="10.5555/live-pdf-degradation",
            title="PDF Only Degradation Replay",
            authors=["Grace Hopper"],
            year=2026,
            normalized_extra={
                "extraction_method": "pdf_text",
                "parse_warnings": [
                    "raw/papers/2601.10005v1.pdf: PDF-only degradation; rerun dual-format arXiv acquisition"
                ],
            },
        )
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        return workspace

    def live_issue_openalex_records(self) -> dict[str, dict[str, Any]]:
        return {
            "W260110001": self.openalex_work(
                openalex_id="W260110001",
                doi="10.5555/live-name-form",
                title="Author Name Form Replay",
                authors=["J. J. Miller", "Mickaël Seznec", "Wu, Hao", "Oluwasanmi Koyejo"],
                year=2026,
            ),
            "W260110002": self.openalex_work(
                openalex_id="W260110002",
                doi="10.5555/live-title-lag",
                title="KIVI : Plug-and-play 2bit KV Cache Quantization with Streaming Asymmetric Quantization",
                authors=["Zirui Liu", "Yuan, Jiayi"],
                year=2025,
            ),
            "W260110003": self.openalex_work(
                openalex_id="W260110003",
                doi="10.5555/live-wrong-work",
                title="Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
                authors=["Riley Chen", "Morgan Park"],
                year=2026,
            ),
            "W260110004": self.openalex_work(
                openalex_id="W260110004",
                doi="10.5555/live-public-domain",
                title="Public Domain License Replay",
                authors=["Ada Lovelace"],
                year=2026,
            ),
            "W260110005": self.openalex_work(
                openalex_id="W260110005",
                doi="10.5555/live-pdf-degradation",
                title="PDF Only Degradation Replay",
                authors=["Grace Hopper"],
                year=2026,
            ),
        }

    def live_issue_arxiv_records(self) -> dict[str, dict[str, Any]]:
        return {
            "2601.10001v1": {
                "title": "Author Name Form Replay",
                "authors": ["John Miller", "Mickael Seznec", "Hao Wu", "Sanmi Koyejo"],
                "year": 2026,
            },
            "2601.10002v1": {
                "title": "KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache",
                "authors": ["Zirui Liu", "Jiayi Yuan"],
                "year": 2026,
            },
            "2601.10003v1": {
                "title": "TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
                "authors": ["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
                "year": 2026,
            },
            "2601.10004v1": {
                "title": "Public Domain License Replay",
                "authors": ["Ada Lovelace"],
                "year": 2026,
            },
            "2601.10005v1": {
                "title": "PDF Only Degradation Replay",
                "authors": ["Grace Hopper"],
                "year": 2026,
            },
        }

    def build_wrong_work_enrichment_workspace(self, root: Path) -> Path:
        workspace = root / "wrong-work-enrichment-replay"
        for relative in ("raw/papers", "sources/normalized", "sources/discovery", "wiki/questions", "wiki/synthesis"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        config = {
            "project": {"name": "academic-wrong-work-enrichment-replay"},
            "raw": {"source_roots": ["raw/papers"]},
            "sources": {
                "manifest_path": "sources/manifest.jsonl",
                "normalized_dir": "sources/normalized",
                "source_requests_path": "sources/source-requests.jsonl",
                "coverage_dir": "sources/coverage",
            },
            "wiki": {"root": "wiki"},
            "integrations": {
                "acquisition": {
                    "enabled": True,
                    "providers": ["openalex"],
                    "target_root": "raw/papers",
                    "max_downloads_per_run": 5,
                },
                "discovery": {"enabled": True},
            },
            "lint": {
                "validate_structure": False,
                "validate_frontmatter": False,
                "validate_links": False,
                "validate_source_coverage": False,
                "validate_claims": False,
                "validate_questions": False,
                "validate_source_requests": False,
                "validate_curation_metadata": False,
                "validate_output_license_status": False,
                "detect_prompt_injection_patterns": False,
                "validate_provenance": True,
                "validate_academic_publication_metadata": True,
            },
        }
        self.write_health_complete_config(workspace, config)

        arxiv_id = "2504.19874v1"
        source_id = f"paper:{arxiv_id}"
        raw_relative = f"raw/papers/{arxiv_id}.pdf"
        raw_path = workspace / raw_relative
        raw_path.write_bytes(b"PDF replay fixture for 2504.19874v1\n")
        checksum = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
        provenance = {
            "origin_url": f"https://arxiv.org/abs/{arxiv_id}",
            "retrieved_at": "2026-07-09T00:00:00Z",
            "retrieved_by": "fetch_sources.py/arxiv",
            "license": "CC-BY-4.0",
            "terms_url": f"https://arxiv.org/abs/{arxiv_id}",
            "checksum": checksum,
            "checksum_verified": True,
            "sidecar_path": f"{raw_relative}.provenance.yml",
            "request_id": "req-wrong-work-enrichment-replay",
            "candidate_id": "cand-2504-19874v1",
            "academic_provider": "arxiv",
            "academic_source_type": "preprint",
            "venue": "arXiv",
            "arxiv_id": arxiv_id,
            "publication_year": 2025,
            "oa_status": "green",
            "peer_review_status": "preprint",
            "title": "TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
            "authors": ["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
            "published": "2025-04-28T00:00:00Z",
            "doi": "10.48550/arXiv.2504.19874",
            "doi_source": "datacite-derived",
        }
        (workspace / f"{raw_relative}.provenance.yml").write_text(
            yaml.safe_dump(provenance, sort_keys=False),
            encoding="utf-8",
        )
        normalized = {
            "type": "normalized_source",
            "source_id": source_id,
            "source_kind": "paper",
            "status": "content_extracted",
            "evidence_usable": True,
            "created": "2026-07-09",
            "updated": "2026-07-09",
            "normalized_at": "2026-07-09T00:00:00Z",
            "raw_paths": [raw_relative],
            "raw_pdf": raw_relative,
            "manifest_path": "sources/manifest.jsonl",
            "normalizer": {"name": "normalize_sources.py", "version": 1},
            "parse_warnings": [],
            "title": "TurboQuant: Online Vector Quantization with Near-optimal Distortion Rate",
            "title_source": "provider",
            "authors": ["Amir Zandieh", "Majid Daliri", "Majid Hadian", "Vahab Mirrokni"],
            "publication_year": 2025,
            "arxiv_id": arxiv_id,
            "doi": "10.48550/arXiv.2504.19874",
            "provenance": provenance,
        }
        write_frontmatter(workspace / "sources" / "normalized" / "paper--2504.19874v1.md", normalized)
        record = {
            "id": source_id,
            "kind": "paper",
            "status": "normalized",
            "raw_paths": [raw_relative],
            "detected_at": "2026-07-09T00:00:00Z",
            "provenance": provenance,
        }
        (workspace / "sources" / "manifest.jsonl").write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        return workspace

    def run_openalex_enrich(self, workspace: Path) -> dict[str, Any]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = VERIFY.fetch_sources.main(
                [
                    "--project-root",
                    str(workspace),
                    "--format",
                    "json",
                    "openalex",
                    "enrich",
                    "--source-id",
                    "paper:2504.19874v1",
                ]
            )
        if code != 0:
            raise AssertionError(stderr.getvalue() or stdout.getvalue())
        return json.loads(stdout.getvalue())

    def wrong_work_openalex_record(self) -> dict[str, Any]:
        return self.openalex_work(
            openalex_id="W4416915242",
            doi="10.48550/arXiv.2504.19874",
            title="Formal Verification of TurboQuant: Machine-Checked Proofs and Gap Closures",
            authors=["Riley Chen", "Morgan Park"],
            year=2025,
        )

    def build_paired_latex_workspace(self, root: Path) -> tuple[Path, list[dict[str, Any]], dict[str, int]]:
        workspace = root / "paired-latex-replay"
        for relative in ("raw/pdf", "raw/other/arXiv-2601.20001v1", "sources/normalized", "sources/discovery", "wiki/questions", "wiki/synthesis"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        (workspace / "research.yml").write_text(
            yaml.safe_dump(
                {
                    "project": {"name": "paired-latex-replay"},
                    "raw": {"source_roots": ["raw/pdf", "raw/other"]},
                    "sources": {
                        "manifest_path": "sources/manifest.jsonl",
                        "normalized_dir": "sources/normalized",
                        "source_requests_path": "sources/source-requests.jsonl",
                        "coverage_dir": "sources/coverage",
                    },
                    "wiki": {"root": "wiki"},
                    "integrations": {
                        "acquisition": {
                            "enabled": True,
                            "providers": ["arxiv", "openalex"],
                            "target_root": "raw/papers",
                            "max_downloads_per_run": 5,
                        },
                        "discovery": {"enabled": True},
                    },
                    "lint": {
                        "validate_structure": False,
                        "validate_frontmatter": False,
                        "validate_links": False,
                        "validate_source_coverage": False,
                        "validate_claims": False,
                        "validate_questions": False,
                        "validate_source_requests": False,
                        "validate_curation_metadata": False,
                        "validate_output_license_status": False,
                        "detect_prompt_injection_patterns": False,
                        "validate_provenance": True,
                        "validate_academic_publication_metadata": True,
                    },
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
        self.write_health_complete_config(workspace, config)
        bundle = workspace / "raw" / "other" / "arXiv-2601.20001v1"
        (bundle / "00README.json").write_text(json.dumps({"entrypoint": "main.tex"}, sort_keys=True), encoding="utf-8")
        (bundle / "main.tex").write_text(
            "\\documentclass{article}\n"
            "\\title{Paired Latex Replay Paper}\n"
            "\\author{Ada Lovelace \\and Grace Hopper}\n"
            "\\begin{document}\n"
            "\\maketitle\n"
            "\\begin{abstract}\n"
            "This paired LaTeX replay source verifies that inventory pairing and LaTeX normalization feed the ship gate.\n"
            "\\end{abstract}\n"
            "\\section{Introduction}\n"
            "The normalized text comes from the LaTeX entrypoint rather than the paired PDF fallback.\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
        pdf = workspace / "raw" / "pdf" / "2601.20001v1.pdf"
        pdf.write_bytes(b"PDF pair replay fixture\n")
        checksum = "sha256:" + hashlib.sha256(pdf.read_bytes()).hexdigest()
        (workspace / "raw" / "pdf" / "2601.20001v1.pdf.provenance.yml").write_text(
            yaml.safe_dump(
                {
                    "origin_url": "https://arxiv.org/abs/2601.20001v1",
                    "retrieved_at": "2026-07-09T00:00:00Z",
                    "retrieved_by": "fetch_sources.py/arxiv",
                    "license": "CC-BY-4.0",
                    "terms_url": "https://arxiv.org/abs/2601.20001v1",
                    "checksum": checksum,
                    "checksum_verified": True,
                    "academic_provider": "arxiv",
                    "academic_source_type": "preprint",
                    "venue": "arXiv",
                    "arxiv_id": "2601.20001v1",
                    "doi": "10.5555/paired-latex-replay",
                    "openalex_work_id": "W260120001",
                    "publication_year": 2026,
                    "oa_status": "green",
                    "peer_review_status": "preprint",
                    "title": "Paired Latex Replay Paper",
                    "authors": ["Ada Lovelace", "Grace Hopper"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        config = INVENTORY.load_config(workspace)
        records, _warnings, summary = INVENTORY.build_records(workspace, config, previous_detected_at={})
        INVENTORY.write_manifest(workspace / "sources" / "manifest.jsonl", records)
        return workspace, records, summary

    def add_vendor_source(self, workspace: Path, records: list[dict[str, Any]], *, broken: bool) -> None:
        raw_relative = "raw/web/vendor-product.html"
        raw_path = workspace / raw_relative
        raw_path.write_text("<html><body>Vendor-controlled product specification.</body></html>\n", encoding="utf-8")
        checksum = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
        provenance = {
            "origin_url": "https://vendor.example/product/spec",
            "retrieved_at": "2026-07-05T00:00:00Z",
            "retrieved_by": "fetch_sources.py/web",
            "license": "CC-BY-4.0",
            "terms_url": "https://vendor.example/terms",
            "notes": "Official vendor product specification.",
            "checksum": checksum,
            "checksum_verified": True,
            "request_id": "req-vendor-product",
            "candidate_id": "cand-vendor-product",
            "source_type": "web_page",
            "publisher": "Vendor Example",
            "supported_evidence_areas": ["vendor_product_spec"],
        }
        (workspace / f"{raw_relative}.provenance.yml").write_text(
            yaml.safe_dump(provenance, sort_keys=False),
            encoding="utf-8",
        )
        write_frontmatter(
            workspace / "sources" / "normalized" / "web--vendor-product.md",
            {
                "type": "normalized_source",
                "source_id": "web:vendor-product",
                "source_kind": "html",
                "status": "content_extracted",
                "evidence_usable": True,
                "created": "2026-07-05",
                "updated": "2026-07-05",
                "normalized_at": "2026-07-05T00:00:00Z",
                "raw_paths": [raw_relative],
                "manifest_path": "sources/manifest.jsonl",
                "normalizer": {"name": "normalize_sources.py", "version": 1},
                "parse_warnings": [],
                "title": "Official Product Spec",
                "provenance": provenance,
            },
        )
        records.append(
            {
                "id": "web:vendor-product",
                "kind": "html",
                "status": "normalized",
                "raw_paths": [raw_relative],
                "detected_at": "2026-07-05T00:00:00Z",
                "url": "https://vendor.example/product/spec",
                "provenance": provenance,
            }
        )
        candidate = {
            "schema_version": "1.0",
            "candidate_id": "cand-vendor-product",
            "provider": "search",
            "url": "https://vendor.example/product/spec",
            "title": "Official Product Spec",
            "source_type": "web_page",
            "trust_tier": "official_primary",
            "official_source": True,
            "recommended_action": "fetch",
            "status": "new" if broken else "selected",
            "selected_for_request_id": None if broken else "req-vendor-product",
            "selection_reason": None if broken else "official_primary trust tier satisfies the linked source policy",
            "evidence_path": "vendor_product_spec",
            "source_policy": "official_vendor",
            "freshness_policy": "current_product_spec",
            "identity_policy": "origin_url_matches_candidate",
        }
        (workspace / "sources" / "discovery" / "candidates.jsonl").write_text(
            json.dumps(candidate, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def verify_report(self, workspace: Path, provider: str) -> dict[str, Any]:
        return VERIFY.build_report(
            workspace,
            argparse.Namespace(source_id=None, live=True, provider=provider),
        )

    def lint_report(self, workspace: Path) -> dict[str, Any]:
        config = yaml.safe_load((workspace / "research.yml").read_text(encoding="utf-8"))
        return LINT.run_checks(workspace, config)

    def status_input(self) -> dict[str, Any]:
        return {
            "readiness": {"verdict": "complete", "reasons": [], "verdict_reasons": []},
            "candidates": {
                "invalid_records": 0,
                "selection": {"selected_without_request": 0},
                "rejections": {"missing_reason": 0},
            },
            "coverage": {},
        }

    def export_input(self, *, manual_review: bool = False) -> dict[str, Any]:
        facet_results = []
        if manual_review:
            facet_results = [
                {
                    "facet_id": "official-spec",
                    "policy_results": [
                        {
                            "policy": "manual_review_required",
                            "verdict": "manual_review",
                            "reasons": ["Candidate lifecycle was not completed."],
                        }
                    ],
                }
            ]
        return {
            "counts": {"questions": 1},
            "warnings": [],
            "questions": [
                {
                    "slug": "academic-replay",
                    "status": "answered",
                    "coverage_required": True,
                    "coverage_status": "pass",
                    "coverage_facets": facet_results,
                    "citations": [],
                }
            ],
        }

    def readiness_document(
        self,
        workspace: Path,
        *,
        lint_report: dict[str, Any],
        citation_report: dict[str, Any],
        manual_review: bool = False,
    ) -> dict[str, Any]:
        return READINESS.build_readiness_document(
            workspace,
            embedded_inputs={
                "status": self.status_input(),
                "lint": lint_report,
                "export": self.export_input(manual_review=manual_review),
                "citation_verification": citation_report,
            },
        )

    def test_fixed_academic_shape_reaches_ship_with_mocked_live_providers(self):
        self.install_provider_fixtures()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir))
            arxiv = self.verify_report(workspace, "arxiv")
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            arxiv_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=arxiv,
            )
            openalex_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
            )

        self.assertEqual("verified", arxiv["overall_result"])
        self.assertEqual({"verified": 11, "mismatch": 0, "not_found": 0, "skipped_no_live": 0, "insufficient_metadata": 0, "total": 11}, arxiv["counts"])
        self.assertEqual("verified", openalex["overall_result"])
        self.assertEqual({"verified": 11, "mismatch": 0, "not_found": 0, "skipped_no_live": 0, "insufficient_metadata": 0, "total": 11}, openalex["counts"])
        self.assertFalse(any(issue.get("severity") == "MEDIUM" for issue in lint_report["issues"]))
        self.assertEqual("ship", arxiv_readiness["verdict"])
        self.assertEqual("ship", openalex_readiness["verdict"])
        self.assertEqual([], arxiv_readiness["reasons"]["coverage"])
        self.assertEqual([], openalex_readiness["reasons"]["coverage"])

    def test_live_issue_classes_replay_as_ship_with_recorded_evidence(self):
        self.install_arxiv_records(self.live_issue_arxiv_records())
        self.install_openalex_records(self.live_issue_openalex_records())
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_live_issue_workspace(Path(tmpdir))
            arxiv = self.verify_report(workspace, "arxiv")
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            arxiv_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=arxiv,
            )
            openalex_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
            )

        arxiv_by_source = {result["source_id"]: result for result in arxiv["results"]}
        by_source = {result["source_id"]: result for result in openalex["results"]}
        self.assertEqual("verified", arxiv["overall_result"])
        self.assertEqual(
            {"verified": 5, "mismatch": 0, "not_found": 0, "skipped_no_live": 0, "insufficient_metadata": 0, "total": 5},
            arxiv["counts"],
        )
        self.assertEqual("verified", openalex["overall_result"])
        self.assertEqual(
            {"verified": 5, "mismatch": 0, "not_found": 0, "skipped_no_live": 0, "insufficient_metadata": 0, "total": 5},
            openalex["counts"],
        )
        self.assertEqual("verified", arxiv_by_source["paper:2601.10001v1"]["result"])
        self.assertTrue(by_source["paper:2601.10001v1"]["comparisons"]["authors"]["matched"])
        self.assertEqual(
            [
                {"local": "john miller", "provider": "j j miller", "rule": "family_given_compatible"},
                {"local": "mickael seznec", "provider": "mickael seznec", "rule": "canonical_tokens"},
                {"local": "hao wu", "provider": "hao wu", "rule": "canonical_tokens"},
                {"local": "sanmi koyejo", "provider": "oluwasanmi koyejo", "rule": "family_given_compatible"},
            ],
            by_source["paper:2601.10001v1"]["comparisons"]["authors"]["matches"],
        )
        self.assertTrue(
            any("openalex_title_version_lag" in reason for reason in by_source["paper:2601.10002v1"]["reasons"])
        )
        self.assertTrue(
            any(
                "openalex_identity_conflict_recorded" in reason
                for reason in by_source["paper:2601.10003v1"]["reasons"]
            )
        )
        self.assertTrue(
            any(
                "openalex_identity_quorum_verified" in reason
                for reason in by_source["paper:2601.10003v1"]["reasons"]
            )
        )
        self.assertFalse(
            any(
                issue.get("category") == "provenance_missing_license" and issue.get("source_id") == "paper:2601.10004v1"
                for issue in lint_report["issues"]
            )
        )
        self.assertFalse(any(issue.get("severity") == "MEDIUM" for issue in lint_report["issues"]))
        self.assertEqual("ship", arxiv_readiness["verdict"])
        self.assertEqual("ship", openalex_readiness["verdict"])
        self.assertEqual([], arxiv_readiness["reasons"]["citation_identity"])
        self.assertEqual([], openalex_readiness["reasons"]["citation_identity"])
        self.assertEqual([], openalex_readiness["reasons"]["source_quality"])

    def test_paired_latex_workspace_reaches_ship_through_inventory_and_normalization(self):
        arxiv_id = "2601.20001v1"
        title = "Paired Latex Replay Paper"
        authors = ["Ada Lovelace", "Grace Hopper"]
        self.install_arxiv_records({arxiv_id: {"title": title, "authors": authors, "year": 2026}})
        self.install_openalex_records(
            {
                "W260120001": self.openalex_work(
                    openalex_id="W260120001",
                    doi="10.5555/paired-latex-replay",
                    title=title,
                    authors=authors,
                    year=2026,
                )
            }
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace, records, summary = self.build_paired_latex_workspace(Path(tmpdir))
            paper = next(record for record in records if record["id"] == f"paper:{arxiv_id}")
            normalization_method = NORMALIZE.normalization_method(workspace, paper)
            normalized = NORMALIZE.normalize_latex_record(workspace, paper)
            output_path = NORMALIZE.normalized_output_path_for_record(paper, workspace / "sources" / "normalized")
            frontmatter = NORMALIZE.frontmatter_for(
                normalized,
                "sources/manifest.jsonl",
                output_path,
                "2026-07-09",
                manifest_records=records,
                project_root=workspace,
            )
            output_path.write_text(NORMALIZE.render_markdown(normalized, frontmatter), encoding="utf-8")
            arxiv = self.verify_report(workspace, "arxiv")
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            arxiv_readiness = self.readiness_document(workspace, lint_report=lint_report, citation_report=arxiv)
            openalex_readiness = self.readiness_document(workspace, lint_report=lint_report, citation_report=openalex)

        self.assertEqual({"paired": 1, "pdf_only": 0, "latex_only": 0, "ambiguous": 0}, summary)
        self.assertEqual("paired", paper["pairing_status"])
        self.assertEqual("raw/pdf/2601.20001v1.pdf", paper["raw_pdf"])
        self.assertEqual("arxiv", paper["metadata"]["bundle_type"])
        self.assertEqual("latex", normalization_method)
        self.assertEqual("latex", normalized.extraction_method)
        self.assertEqual("latex", frontmatter["extraction_method"])
        self.assertIn("raw/other/arXiv-2601.20001v1/main.tex", normalized.included_paths)
        self.assertEqual("verified", arxiv["overall_result"])
        self.assertEqual("verified", openalex["overall_result"])
        self.assertEqual("ship", arxiv_readiness["verdict"])
        self.assertEqual("ship", openalex_readiness["verdict"])

    def test_wrong_work_replay_drives_real_enrichment_to_verified_ship_when_doi_corroborates(self):
        expected_identifier = "doi:10.48550/arxiv.2504.19874"
        self.install_single_openalex_work(self.wrong_work_openalex_record(), expected_identifier=expected_identifier)
        self.install_doi_resolution("https://arxiv.org/abs/2504.19874")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_wrong_work_enrichment_workspace(Path(tmpdir))
            enrich = self.run_openalex_enrich(workspace)
            sidecar = yaml.safe_load(
                (workspace / "raw" / "papers" / "2504.19874v1.pdf.provenance.yml").read_text(encoding="utf-8")
            )
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
            )

        by_source = {result["source_id"]: result for result in openalex["results"]}
        result = by_source["paper:2504.19874v1"]
        self.assertEqual(1, enrich["resolved_count"])
        self.assertTrue(sidecar["openalex_identity_conflict"])
        self.assertEqual("resolved", sidecar["doi_resolution"]["status"])
        self.assertTrue(sidecar["doi_resolution"]["matches_arxiv_id"])
        self.assertEqual("verified", openalex["overall_result"])
        self.assertEqual("verified", result["result"])
        self.assertTrue(any("openalex_identity_conflict_recorded" in reason for reason in result["reasons"]))
        self.assertTrue(any("openalex_identity_quorum_verified" in reason for reason in result["reasons"]))
        self.assertEqual("ship", readiness["verdict"])
        self.assertEqual([], readiness["reasons"]["citation_identity"])

    def test_wrong_work_replay_drives_real_enrichment_to_no_ship_when_doi_does_not_corroborate(self):
        expected_identifier = "doi:10.48550/arxiv.2504.19874"
        self.install_single_openalex_work(self.wrong_work_openalex_record(), expected_identifier=expected_identifier)
        self.install_doi_resolution("https://example.org/not-the-paper")
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_wrong_work_enrichment_workspace(Path(tmpdir))
            enrich = self.run_openalex_enrich(workspace)
            sidecar = yaml.safe_load(
                (workspace / "raw" / "papers" / "2504.19874v1.pdf.provenance.yml").read_text(encoding="utf-8")
            )
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
            )

        by_source = {result["source_id"]: result for result in openalex["results"]}
        result = by_source["paper:2504.19874v1"]
        self.assertEqual(1, enrich["resolved_count"])
        self.assertTrue(sidecar["openalex_identity_conflict"])
        self.assertEqual("redirect_mismatch", sidecar["doi_resolution"]["status"])
        self.assertFalse(sidecar["doi_resolution"]["matches_arxiv_id"])
        self.assertEqual("no_ship", openalex["overall_result"])
        self.assertEqual("mismatch", result["result"])
        self.assertTrue(any("openalex_identity_conflict_uncorroborated" in reason for reason in result["reasons"]))
        self.assertEqual("no_ship", readiness["verdict"])
        self.assertTrue(any("mismatch" in reason for reason in readiness["reasons"]["citation_identity"]))

    def test_unrecorded_openalex_wrong_work_still_replays_no_ship(self):
        self.install_openalex_records(self.live_issue_openalex_records())
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_live_issue_workspace(Path(tmpdir), record_conflict=False)
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
            )

        conflict = next(result for result in openalex["results"] if result["source_id"] == "paper:2601.10003v1")
        self.assertEqual("no_ship", openalex["overall_result"])
        self.assertEqual("mismatch", conflict["result"])
        self.assertTrue(any("openalex_identity_conflict_unrecorded" in reason for reason in conflict["reasons"]))
        self.assertEqual("no_ship", readiness["verdict"])
        self.assertTrue(
            any("mismatch" in reason for reason in readiness["reasons"]["citation_identity"])
        )

    def test_negative_control_replays_historical_academic_no_ship_signals(self):
        self.install_provider_fixtures()
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            workspace = self.build_workspace(Path(tmpdir), broken=True)
            arxiv = self.verify_report(workspace, "arxiv")
            openalex = self.verify_report(workspace, "openalex")
            lint_report = self.lint_report(workspace)
            arxiv_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=arxiv,
                manual_review=True,
            )
            openalex_readiness = self.readiness_document(
                workspace,
                lint_report=lint_report,
                citation_report=openalex,
                manual_review=True,
            )

        self.assertEqual("no_ship", arxiv["overall_result"])
        self.assertTrue(any(result["result"] == "mismatch" for result in arxiv["results"]))
        self.assertEqual("no_ship", openalex["overall_result"])
        self.assertTrue(any(result["result"] == "insufficient_metadata" for result in openalex["results"]))
        self.assertTrue(
            any(
                issue.get("category") == "provenance_missing_license" and issue.get("severity") == "MEDIUM"
                for issue in lint_report["issues"]
            )
        )
        self.assertEqual("no_ship", arxiv_readiness["verdict"])
        self.assertEqual("no_ship", openalex_readiness["verdict"])
        self.assertTrue(any("mismatch" in reason for reason in arxiv_readiness["reasons"]["citation_identity"]))
        self.assertTrue(
            any("insufficient_metadata" in reason for reason in openalex_readiness["reasons"]["citation_identity"])
        )
        self.assertTrue(any("provenance" in reason.lower() for reason in arxiv_readiness["reasons"]["source_quality"]))
        self.assertTrue(any("manual_review" in reason for reason in arxiv_readiness["reasons"]["coverage"]))


if __name__ == "__main__":
    unittest.main()
