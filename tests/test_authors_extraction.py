"""Tests for author extraction from normalized paper sources (E35-T01).

`discover_sources.py authors --source-id` reads a normalized paper source (and any
provider author metadata captured on the manifest record) and emits a bounded
author seed list with provenance (`source`) and `confidence`. It is read-only
(`network_io_executed: false`), never contacts a provider, and never infers
personal data — only metadata already present in the source/provider response is
surfaced. Covers the pure extraction helpers (local frontmatter, arXiv, OpenAlex)
and the end-to-end command (merge, bound, gate, unknown source).
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


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DISCOVER = load_script_module("discover_sources_authors_under_test", "discover_sources.py")


class AuthorExtractionHelperTests(unittest.TestCase):
    """The pure extraction/normalization helpers."""

    def test_normalize_orcid_from_bare_id_and_url(self):
        self.assertEqual("0000-0002-1825-0097", DISCOVER.normalize_orcid("0000-0002-1825-0097"))
        self.assertEqual("0000-0002-1825-0097", DISCOVER.normalize_orcid("https://orcid.org/0000-0002-1825-0097"))
        self.assertEqual("0000-0001-5109-376X", DISCOVER.normalize_orcid("orcid.org/0000-0001-5109-376X"))
        self.assertIsNone(DISCOVER.normalize_orcid("not-an-orcid"))
        self.assertIsNone(DISCOVER.normalize_orcid(None))

    def test_local_frontmatter_extraction_uses_record_confidence(self):
        seeds = DISCOVER.extract_authors_from_frontmatter(
            {"authors": ["Grace Hopper", "  Margaret  Hamilton "], "confidence": "high"}
        )
        self.assertEqual(["Grace Hopper", "Margaret Hamilton"], [s["name"] for s in seeds])
        self.assertTrue(all(s["source"] == "normalized_frontmatter" for s in seeds))
        self.assertTrue(all(s["confidence"] == "high" for s in seeds))
        self.assertTrue(all(s["orcid"] is None and s["affiliation"] is None for s in seeds))

    def test_frontmatter_without_authors_or_bad_confidence(self):
        self.assertEqual([], DISCOVER.extract_authors_from_frontmatter({"title": "x"}))
        seeds = DISCOVER.extract_authors_from_frontmatter({"authors": ["A"], "confidence": "bogus"})
        self.assertEqual("medium", seeds[0]["confidence"])  # unknown tier -> medium

    def test_arxiv_extraction_names_and_optional_affiliation(self):
        seeds = DISCOVER.extract_authors_from_arxiv(
            {"authors": ["Jane Roe", {"name": "John Doe", "affiliation": "MIT"}]}
        )
        self.assertEqual(["Jane Roe", "John Doe"], [s["name"] for s in seeds])
        self.assertTrue(all(s["source"] == "arxiv" and s["confidence"] == "high" for s in seeds))
        self.assertIsNone(seeds[0]["affiliation"])
        self.assertEqual("MIT", seeds[1]["affiliation"])

    def test_openalex_extraction_orcid_and_affiliation(self):
        seeds = DISCOVER.extract_authors_from_openalex(
            {
                "authorships": [
                    {
                        "author": {"display_name": "Ada Lovelace", "orcid": "https://orcid.org/0000-0002-1825-0097"},
                        "institutions": [{"display_name": "Analytical Engine Lab"}],
                    },
                    {
                        "author": {"display_name": "Charles Babbage"},
                        "raw_affiliation_strings": ["Cambridge"],
                    },
                ]
            }
        )
        ada, charles = seeds
        self.assertEqual("Ada Lovelace", ada["name"])
        self.assertEqual("0000-0002-1825-0097", ada["orcid"])
        self.assertEqual("Analytical Engine Lab", ada["affiliation"])
        self.assertEqual("openalex", ada["source"])
        self.assertIsNone(charles["orcid"])
        self.assertEqual("Cambridge", charles["affiliation"])  # raw string fallback

    def test_provider_shape_detection(self):
        self.assertEqual("openalex", DISCOVER.extract_authors_from_provider_metadata(
            {"authorships": [{"author": {"display_name": "A"}}]}
        )[0]["source"])
        self.assertEqual("arxiv", DISCOVER.extract_authors_from_provider_metadata(
            {"authors": ["A"]}
        )[0]["source"])
        self.assertEqual([], DISCOVER.extract_authors_from_provider_metadata({"other": 1}))
        self.assertEqual([], DISCOVER.extract_authors_from_provider_metadata(None))

    def test_merge_prefers_provider_and_fills_missing_fields(self):
        provider = DISCOVER.extract_authors_from_openalex(
            {"authorships": [{"author": {"display_name": "Ada Lovelace", "orcid": "0000-0002-1825-0097"}}]}
        )
        frontmatter = DISCOVER.extract_authors_from_frontmatter(
            {"authors": ["ada lovelace", "Grace Hopper"], "confidence": "medium"}
        )
        merged = DISCOVER.merge_author_seeds(provider, frontmatter, max_results=50)
        by_name = {s["name"]: s for s in merged}
        # Case-folded dedupe: one Ada, from the richer provider seed.
        self.assertEqual(2, len(merged))
        self.assertEqual("0000-0002-1825-0097", by_name["Ada Lovelace"]["orcid"])
        self.assertEqual("openalex", by_name["Ada Lovelace"]["source"])
        self.assertEqual("normalized_frontmatter", by_name["Grace Hopper"]["source"])

    def test_merge_is_bounded(self):
        seeds = [DISCOVER.author_seed(f"Author {i}", source="arxiv", confidence="high") for i in range(20)]
        self.assertEqual(5, len(DISCOVER.merge_author_seeds(seeds, max_results=5)))

    def test_author_seed_rejects_empty_name(self):
        self.assertIsNone(DISCOVER.author_seed("   ", source="arxiv", confidence="high"))
        self.assertIsNone(DISCOVER.author_seed(None, source="arxiv", confidence="high"))


class AuthorsCommandTests(unittest.TestCase):
    """End-to-end through `discover_sources.py authors`."""

    def write_workspace(
        self,
        root: Path,
        *,
        manifest: list[dict],
        normalized: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> Path:
        workspace = root / "ws"
        (workspace / "sources" / "normalized").mkdir(parents=True, exist_ok=True)
        lines = [
            "project:",
            "  name: authors-fixture",
            "sources:",
            "  manifest_path: sources/manifest.jsonl",
            "  normalized_dir: sources/normalized",
            "integrations:",
            "  discovery:",
            f"    enabled: {'true' if enabled else 'false'}",
        ]
        (workspace / "research.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (workspace / "sources" / "manifest.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in manifest), encoding="utf-8"
        )
        for filename, body in (normalized or {}).items():
            (workspace / "sources" / "normalized" / filename).write_text(body, encoding="utf-8")
        return workspace

    def frontmatter_doc(self, source_id: str, authors: list[str], confidence: str = "high") -> str:
        author_lines = "\n".join(f"  - {a}" for a in authors)
        return (
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            "authors:\n"
            f"{author_lines}\n"
            f"confidence: {confidence}\n"
            "---\n\n# body\n"
        )

    def run_authors(self, workspace: Path, *args: str) -> tuple[int, str, str]:
        argv = ["--project-root", str(workspace), "--format", "json", "authors", *args]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = DISCOVER.main(argv)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def test_local_paper_emits_seed_list_with_provenance_and_confidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[{"id": "paper:local-1", "kind": "paper", "status": "normalized"}],
                normalized={"paper--local-1.md": self.frontmatter_doc("paper:local-1", ["Grace Hopper", "Margaret Hamilton"])},
            )
            code, stdout, stderr = self.run_authors(workspace, "--source-id", "paper:local-1")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("authors", report["command"])
        self.assertEqual("paper:local-1", report["source_id"])
        self.assertFalse(report["network_io_executed"])
        self.assertEqual(2, report["count"])
        self.assertEqual(["Grace Hopper", "Margaret Hamilton"], [a["name"] for a in report["authors"]])
        self.assertTrue(all(a["source"] == "normalized_frontmatter" for a in report["authors"]))
        self.assertEqual([], report["warnings"])

    def test_openalex_metadata_enriches_frontmatter_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[{
                    "id": "paper:oa-1",
                    "kind": "paper",
                    "status": "normalized",
                    "metadata": {"authorships": [
                        {"author": {"display_name": "Ada Lovelace", "orcid": "https://orcid.org/0000-0002-1825-0097"},
                         "institutions": [{"display_name": "Analytical Engine Lab"}]},
                    ]},
                }],
                normalized={"paper--oa-1.md": self.frontmatter_doc("paper:oa-1", ["Ada Lovelace"], confidence="medium")},
            )
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:oa-1")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertEqual(1, report["count"])  # frontmatter name deduped into provider seed
        ada = report["authors"][0]
        self.assertEqual("Ada Lovelace", ada["name"])
        self.assertEqual("0000-0002-1825-0097", ada["orcid"])
        self.assertEqual("Analytical Engine Lab", ada["affiliation"])
        self.assertEqual("openalex", ada["source"])

    def test_max_results_bounds_the_seed_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[{"id": "paper:many", "kind": "paper", "status": "normalized"}],
                normalized={"paper--many.md": self.frontmatter_doc("paper:many", [f"Author {i}" for i in range(8)])},
            )
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:many", "--max-results", "3")
        self.assertEqual(0, code)
        self.assertEqual(3, json.loads(stdout)["count"])

    def test_missing_normalized_record_warns_but_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[{"id": "paper:bare", "kind": "paper", "status": "classified"}],
            )
            code, stdout, _ = self.run_authors(workspace, "--source-id", "paper:bare")
        self.assertEqual(0, code)
        report = json.loads(stdout)
        self.assertIsNone(report["normalized_record"])
        codes = {w["code"] for w in report["warnings"]}
        self.assertIn("no_normalized_record", codes)
        self.assertIn("no_author_metadata", codes)
        self.assertEqual(0, report["count"])

    def test_unknown_source_id_is_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[{"id": "paper:x", "kind": "paper"}])
            code, stdout, stderr = self.run_authors(workspace, "--source-id", "paper:missing")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual("SOURCE_UNKNOWN", json.loads(stderr)["error_code"])

    def test_disabled_discovery_refuses(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(
                Path(tmpdir),
                manifest=[{"id": "paper:local-1", "kind": "paper"}],
                normalized={"paper--local-1.md": self.frontmatter_doc("paper:local-1", ["A"])},
                enabled=False,
            )
            code, _, stderr = self.run_authors(workspace, "--source-id", "paper:local-1")
        self.assertEqual(2, code)
        self.assertEqual("DISCOVERY_DISABLED", json.loads(stderr)["error_code"])

    def test_empty_source_id_is_value_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.write_workspace(Path(tmpdir), manifest=[{"id": "paper:x", "kind": "paper"}])
            code, _, stderr = self.run_authors(workspace, "--source-id", "   ")
        self.assertEqual(2, code)
        self.assertEqual("VALUE_INVALID", json.loads(stderr)["error_code"])


if __name__ == "__main__":
    unittest.main()
