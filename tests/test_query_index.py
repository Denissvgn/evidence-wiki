import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "query_index.py"


def load_module():
    spec = importlib.util.spec_from_file_location("research_query_index", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


QIDX = load_module()


class QueryIndexTests(unittest.TestCase):
    def build_workspace(self, root: Path) -> Path:
        (root / "research.yml").write_text(
            "wiki:\n  root: wiki\nsources:\n  normalized_dir: sources/normalized\n"
        )
        concepts = root / "wiki" / "concepts"
        concepts.mkdir(parents=True)
        (concepts / "scaling-laws.md").write_text(
            "---\ntype: concept\nsource_ids:\n  - paper:scaling-2024\n"
            "summary: Scaling laws relate model size and loss.\n---\n\n"
            "# Scaling Laws\n\n## Definition\n\n"
            "Scaling laws describe how loss decreases with model size and data.\n\n"
            "## Emergent Abilities\n\nSome abilities emerge at scale.\n"
        )
        (concepts / "tokenization.md").write_text(
            "---\ntype: concept\nsource_ids: []\n---\n\n"
            "# Tokenization\n\nByte pair encoding splits text into subword tokens.\n"
        )
        normalized = root / "sources" / "normalized"
        normalized.mkdir(parents=True)
        (normalized / "paper--scaling-2024.md").write_text(
            "---\ntype: normalized_source\nsource_id: paper:scaling-2024\n"
            "source_kind: paper\nstatus: content_extracted\n---\n\n"
            "# Scaling Paper\n\n"
            "Empirical study of scaling laws and emergent abilities in language models.\n"
        )
        return root

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = QIDX.main(argv)
        return code, buffer.getvalue()

    def run_cli_with_stderr(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = QIDX.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def build_index(self, root: Path, index_path: str | Path | None = None, scope: str = "all"):
        argv = ["build-index", "--project-root", str(root), "--scope", scope]
        if index_path is not None:
            argv.extend(["--index-path", str(index_path)])
        return self.run_cli(argv)

    def configure_retrieval(
        self,
        root: Path,
        *,
        provider: str,
        command: list[str],
        timeout_seconds: float = 30,
    ) -> None:
        config = {
            "wiki": {"root": "wiki"},
            "sources": {"normalized_dir": "sources/normalized"},
            "integrations": {
                "retrieval": {
                    "provider": provider,
                    "command": command,
                    "timeout_seconds": timeout_seconds,
                }
            },
        }
        (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    def configure_semantic_retrieval(
        self,
        root: Path,
        *,
        provider: str,
        transport: str = "command",
        command: list[str] | None = None,
        endpoint: str | None = None,
        timeout_seconds: float = 30,
    ) -> None:
        semantic: dict[str, object] = {
            "enabled": True,
            "provider": provider,
            "transport": transport,
            "timeout_seconds": timeout_seconds,
        }
        if command is not None:
            semantic["command"] = command
        if endpoint is not None:
            semantic["endpoint"] = endpoint
        config = {
            "wiki": {"root": "wiki"},
            "sources": {"normalized_dir": "sources/normalized"},
            "integrations": {"retrieval": {"semantic": semantic}},
        }
        (root / "research.yml").write_text(yaml.safe_dump(config, sort_keys=False))

    def add_citation_graph_records(self, root: Path) -> None:
        normalized = root / "sources" / "normalized"
        (normalized / "paper--scaling-2024.md").write_text(
            "---\n"
            "type: normalized_source\n"
            "source_id: paper:scaling-2024\n"
            "source_kind: paper\n"
            "status: content_extracted\n"
            "references_source_ids:\n"
            "  - paper:related-2024\n"
            "---\n\n"
            "# Scaling Paper\n\n"
            "Empirical study of scaling laws and emergent abilities in language models.\n"
        )
        (normalized / "paper--related-2024.md").write_text(
            "---\n"
            "type: normalized_source\n"
            "source_id: paper:related-2024\n"
            "source_kind: paper\n"
            "status: content_extracted\n"
            "---\n\n"
            "# Related Paper\n\n"
            "A cited companion paper about benchmark design.\n"
        )
        (normalized / "paper--citing-2024.md").write_text(
            "---\n"
            "type: normalized_source\n"
            "source_id: paper:citing-2024\n"
            "source_kind: paper\n"
            "status: content_extracted\n"
            "references_source_ids:\n"
            "  - paper:scaling-2024\n"
            "---\n\n"
            "# Citing Paper\n\n"
            "A later paper that cites the scaling study.\n"
        )

    def add_codebase_evidence_graph(self, root: Path) -> tuple[str, str, list[str]]:
        source_id = "codebase:bounded-fixture"
        normalized_path = "sources/normalized/codebase--bounded-fixture.md"
        (root / normalized_path).write_text(
            "---\n"
            "type: normalized_source\n"
            f"source_id: {source_id}\n"
            "source_kind: codebase_architecture\n"
            "codebase_intake_status: validated\n"
            "codebase_execution_scope: external_worker_only\n"
            "---\n\n"
            "# Bounded Architecture\n\nExternal-worker architecture boundary evidence.\n"
        )
        maintained_paths = [
            "wiki/sources/codebase--bounded-fixture.md",
            "wiki/claims/codebase-boundary.md",
            "wiki/synthesis/codebase-boundary.md",
            "wiki/decisions/codebase-boundary.md",
        ]
        for relative in maintained_paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            page_type = {
                "sources": "source",
                "claims": "claim",
                "synthesis": "synthesis",
                "decisions": "decision",
            }[path.parent.name]
            path.write_text(
                "---\n"
                f"type: {page_type}\n"
                f"source_ids: [{source_id}]\n"
                "---\n\n"
                f"# Maintained {page_type.title()}\n\nBounded architecture interpretation.\n"
            )
        return source_id, normalized_path, maintained_paths

    def result_by_path(self, payload: dict, path: str) -> dict:
        matches = [result for result in payload["results"] if result["path"] == path]
        self.assertEqual(1, len(matches), f"expected one result for {path}")
        return matches[0]

    def test_ranks_wiki_above_normalized_on_shared_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            config = QIDX.load_config(root)
            documents = QIDX.build_index(root, config, "all")
            results = QIDX.rank_documents(documents, "scaling laws emergent", 10)
            self.assertEqual(results[0]["path"], "wiki/concepts/scaling-laws.md")
            self.assertEqual(results[0]["scope"], "wiki")
            self.assertTrue(any(r["scope"] == "normalized" for r in results))

    def test_scope_filtering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            config = QIDX.load_config(root)
            wiki_docs = QIDX.build_index(root, config, "wiki")
            self.assertTrue(all(d.scope == "wiki" for d in wiki_docs))
            normalized_docs = QIDX.build_index(root, config, "normalized")
            self.assertTrue(all(d.scope == "normalized" for d in normalized_docs))

    def test_source_id_exact_match_boost(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            config = QIDX.load_config(root)
            documents = QIDX.build_index(root, config, "all")
            results = QIDX.rank_documents(documents, "paper:scaling-2024", 10)
            self.assertEqual(results[0]["path"], "sources/normalized/paper--scaling-2024.md")

    def test_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            config = QIDX.load_config(root)
            documents = QIDX.build_index(root, config, "all")
            results = QIDX.rank_documents(documents, "scaling laws tokens", 1)
            self.assertEqual(len(results), 1)

    def test_oversized_direct_limit_is_capped(self):
        documents = [
            QIDX.prepare_document(
                QIDX.Document(
                    path=f"wiki/concepts/scaling-{index:03d}.md",
                    scope="wiki",
                    kind="concept",
                    title=f"Scaling {index}",
                    headings=[],
                    source_ids=[],
                    body="Scaling laws describe model behavior.",
                )
            )
            for index in range(QIDX.MAX_QUERY_LIMIT + 5)
        ]

        results = QIDX.rank_documents(documents, "scaling", QIDX.MAX_QUERY_LIMIT + 500)

        self.assertEqual(QIDX.MAX_QUERY_LIMIT, len(results))

    def test_json_output_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling laws"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["query"], "scaling laws")
            self.assertEqual(payload["scope"], "all")
            self.assertEqual(payload["engine"], "lexical")
            self.assertIn("results", payload)
            self.assertGreaterEqual(payload["result_count"], 1)
            first = payload["results"][0]
            for key in ("path", "scope", "kind", "title", "source_ids", "snippet", "score"):
                self.assertIn(key, first)
            self.assertEqual(first["engine"], "lexical")

    def test_json_results_include_one_hop_related_source_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.add_citation_graph_records(root)

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling laws"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            result = self.result_by_path(payload, "wiki/concepts/scaling-laws.md")
            self.assertEqual(
                ["paper:citing-2024", "paper:related-2024"],
                result["related_source_ids"],
            )

    def test_json_results_expose_normalized_and_maintained_codebase_links_bidirectionally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            source_id, normalized_path, maintained_paths = self.add_codebase_evidence_graph(root)

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "bounded architecture"]
            )

            self.assertEqual(0, code)
            payload = json.loads(output)
            matching = [result for result in payload["results"] if source_id in result["source_ids"]]
            self.assertGreaterEqual(len(matching), 2)
            for result in matching:
                self.assertEqual([normalized_path], result["evidence_links"]["normalized_paths"])
                self.assertEqual(sorted(maintained_paths), result["evidence_links"]["maintained_paths"])
                self.assertNotIn(result["path"], result["evidence_links"]["backlinks"])
                self.assertTrue(result["evidence_links"]["backlinks"])

    def test_configured_provider_results_are_hydrated_and_labeled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            provider_path = root / "mock_provider.py"
            request_path = root / "provider-request.json"
            provider_path.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "request = json.load(sys.stdin)\n"
                "Path(sys.argv[1]).write_text(json.dumps(request, sort_keys=True))\n"
                "print(json.dumps({\n"
                "    'schema_version': '1',\n"
                "    'results': [\n"
                "        {\n"
                "            'path': 'sources/normalized/paper--scaling-2024.md',\n"
                "            'score': 42.5,\n"
                "            'snippet': 'semantic provider snippet',\n"
                "            'matched_headings': ['Provider Match'],\n"
                "        },\n"
                "        {'path': 'wiki/concepts/tokenization.md', 'score': 4},\n"
                "    ],\n"
                "}))\n"
            )
            self.configure_retrieval(
                root,
                provider="mock-semantic",
                command=[sys.executable, str(provider_path), str(request_path)],
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "paraphrased semantic query"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["engine"], "mock-semantic")
            self.assertEqual(payload["indexed_documents"], 3)
            self.assertEqual(payload["result_count"], 2)
            self.assertEqual(payload["results"][0]["engine"], "mock-semantic")
            self.assertEqual(payload["results"][0]["path"], "sources/normalized/paper--scaling-2024.md")
            self.assertEqual(payload["results"][0]["scope"], "normalized")
            self.assertEqual(payload["results"][0]["kind"], "paper")
            self.assertEqual(payload["results"][0]["title"], "Scaling Paper")
            self.assertEqual(payload["results"][0]["source_ids"], ["paper:scaling-2024"])
            self.assertEqual(payload["results"][0]["snippet"], "semantic provider snippet")
            self.assertEqual(payload["results"][0]["matched_headings"], ["Provider Match"])

            request = json.loads(request_path.read_text())
            self.assertEqual(request["schema_version"], "1")
            self.assertEqual(request["query"], "paraphrased semantic query")
            self.assertEqual(request["scope"], "all")
            self.assertEqual(request["limit"], 10)
            self.assertEqual(request["project_root"], str(root.resolve()))
            self.assertEqual(
                request["corpus_roots"],
                [
                    {"scope": "wiki", "path": "wiki"},
                    {"scope": "normalized", "path": "sources/normalized"},
                ],
            )
            self.assertEqual(
                sorted(document["path"] for document in request["documents"]),
                [
                    "sources/normalized/paper--scaling-2024.md",
                    "wiki/concepts/scaling-laws.md",
                    "wiki/concepts/tokenization.md",
                ],
            )

    def test_configured_provider_receives_capped_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            provider_path = root / "mock_provider.py"
            request_path = root / "provider-request.json"
            provider_path.write_text(
                "import json\n"
                "import sys\n"
                "from pathlib import Path\n"
                "request = json.load(sys.stdin)\n"
                "Path(sys.argv[1]).write_text(json.dumps(request, sort_keys=True))\n"
                "print(json.dumps({'schema_version': '1', 'results': []}))\n"
            )
            self.configure_retrieval(
                root,
                provider="mock-semantic",
                command=[sys.executable, str(provider_path), str(request_path)],
            )

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--format",
                    "json",
                    "--limit",
                    str(QIDX.MAX_QUERY_LIMIT + 500),
                    "scaling",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual("mock-semantic", payload["engine"])
            request = json.loads(request_path.read_text())
            self.assertEqual(QIDX.MAX_QUERY_LIMIT, request["limit"])

    def test_configured_provider_results_include_related_source_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.add_citation_graph_records(root)
            provider_path = root / "mock_provider.py"
            provider_path.write_text(
                "import json\n"
                "print(json.dumps({\n"
                "    'schema_version': '1',\n"
                "    'results': [{'path': 'sources/normalized/paper--scaling-2024.md', 'score': 7}],\n"
                "}))\n"
            )
            self.configure_retrieval(
                root,
                provider="mock-semantic",
                command=[sys.executable, str(provider_path)],
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "semantic scaling"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            result = self.result_by_path(payload, "sources/normalized/paper--scaling-2024.md")
            self.assertEqual("mock-semantic", result["engine"])
            self.assertEqual(
                ["paper:citing-2024", "paper:related-2024"],
                result["related_source_ids"],
            )

    def test_configured_provider_empty_results_do_not_fall_back_to_lexical(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            provider_path = root / "empty_provider.py"
            provider_path.write_text("import json\nprint(json.dumps({'results': []}))\n")
            self.configure_retrieval(
                root,
                provider="empty-semantic",
                command=[sys.executable, str(provider_path)],
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling laws"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["engine"], "empty-semantic")
            self.assertEqual(payload["result_count"], 0)
            self.assertEqual(payload["results"], [])

    def test_provider_failure_modes_warn_and_fall_back_to_lexical(self):
        cases = [
            (
                "nonzero",
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                30,
            ),
            (
                "timeout",
                [sys.executable, "-c", "import time; time.sleep(1)"],
                0.01,
            ),
            (
                "malformed",
                [sys.executable, "-c", "print('not json')"],
                30,
            ),
            (
                "unsafe-path",
                [
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'results': [{'path': '../outside.md', 'score': 1}]}))",
                ],
                30,
            ),
        ]
        for label, command, timeout_seconds in cases:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as tmpdir:
                    root = self.build_workspace(Path(tmpdir))
                    self.configure_retrieval(
                        root,
                        provider=f"{label}-semantic",
                        command=command,
                        timeout_seconds=timeout_seconds,
                    )

                    code, output, stderr = self.run_cli_with_stderr(
                        ["--project-root", str(root), "--format", "json", "scaling laws"]
                    )

                    self.assertEqual(code, 0)
                    payload = json.loads(output)
                    self.assertEqual(payload["engine"], "lexical")
                    self.assertGreaterEqual(payload["result_count"], 1)
                    self.assertEqual(payload["results"][0]["engine"], "lexical")
                    self.assertIn("retrieval provider", stderr)
                    self.assertIn("using lexical fallback", stderr)

    def test_semantic_command_provider_hybrid_merges_with_lexical_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            provider_path = root / "semantic_provider.py"
            provider_path.write_text(
                "import json\n"
                "request = json.load(__import__('sys').stdin)\n"
                "print(json.dumps({'schema_version': '1', 'results': [\n"
                "  {'path': 'wiki/concepts/tokenization.md', 'score': 99, 'snippet': 'semantic alias match'},\n"
                "  {'path': 'sources/normalized/paper--scaling-2024.md', 'score': 5}\n"
                "]}))\n"
            )
            self.configure_semantic_retrieval(
                root,
                provider="mock-semantic",
                command=[sys.executable, str(provider_path)],
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling laws"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual("hybrid", payload["engine"])
            self.assertEqual("wiki/concepts/tokenization.md", payload["results"][0]["path"])
            self.assertEqual("hybrid", payload["results"][0]["engine"])
            self.assertEqual("semantic alias match", payload["results"][0]["snippet"])
            self.assertTrue((root / ".research-cache" / "semantic-retrieval" / "last-query.json").is_file())

    def test_semantic_http_provider_uses_mocked_transport(self):
        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "schema_version": "1",
                        "results": [{"path": "wiki/concepts/tokenization.md", "score": 10}],
                    }
                ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.configure_semantic_retrieval(
                root,
                provider="mock-http-semantic",
                transport="http",
                endpoint="http://127.0.0.1:9999/search",
            )
            with mock.patch.object(QIDX.urllib.request, "urlopen", return_value=MockResponse()) as urlopen:
                code, output = self.run_cli(
                    ["--project-root", str(root), "--format", "json", "scaling laws"]
                )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual("hybrid", payload["engine"])
            self.assertEqual("wiki/concepts/tokenization.md", payload["results"][0]["path"])
            request = urlopen.call_args.args[0]
            self.assertEqual("POST", request.get_method())
            sent = json.loads(request.data.decode("utf-8"))
            self.assertEqual(".research-cache/semantic-retrieval", sent["semantic_cache_dir"])

    def test_no_matches_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "nonexistentterm"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["result_count"], 0)
            self.assertEqual(payload["results"], [])

    def test_missing_directories_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text(
                "wiki:\n  root: wiki\nsources:\n  normalized_dir: sources/normalized\n"
            )
            config = QIDX.load_config(root)
            documents = QIDX.build_index(root, config, "all")
            self.assertEqual(documents, [])
            results = QIDX.rank_documents(documents, "anything", 10)
            self.assertEqual(results, [])

    def test_missing_config_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(SystemExit):
                QIDX.load_config(Path(tmpdir))

    def test_empty_query_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            with self.assertRaises(SystemExit):
                QIDX.main(["--project-root", str(root), "   "])

    def test_build_index_creates_sqlite_file_and_cli_uses_json_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            index_path = root / ".research-cache" / "query-index.sqlite3"

            code, output = self.build_index(root)

            self.assertEqual(code, 0)
            self.assertTrue(index_path.is_file())
            self.assertIn("Indexed 3 documents", output)

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--index-path",
                    ".research-cache/query-index.sqlite3",
                    "--format",
                    "json",
                    "scaling laws",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["query"], "scaling laws")
            self.assertEqual(payload["scope"], "all")
            self.assertGreaterEqual(payload["result_count"], 1)
            first = payload["results"][0]
            for key in ("path", "scope", "kind", "title", "source_ids", "snippet", "score"):
                self.assertIn(key, first)

    def test_fts_query_pushes_limit_into_sql_without_fetchall(self):
        class NoFetchAllCursor:
            def __init__(self, rows: list[dict]):
                self.rows = rows

            def fetchone(self):
                return self.rows[0]

            def fetchall(self):
                raise AssertionError("query_fts_index must iterate bounded SQL results")

            def __iter__(self):
                return iter(self.rows)

        class FakeConnection:
            def __init__(self):
                self.executed: list[tuple[str, list | tuple]] = []
                self.row_factory = None

            def execute(self, sql: str, params: list | tuple = ()):
                self.executed.append((sql, params))
                if "COUNT(*)" in sql:
                    return NoFetchAllCursor([{"count": 1}])
                return NoFetchAllCursor(
                    [
                        {
                            "path": "wiki/concepts/scaling-laws.md",
                            "scope": "wiki",
                            "kind": "concept",
                            "title": "Scaling Laws",
                            "headings": "Definition",
                            "source_ids": "",
                            "body": "Scaling laws describe model behavior.",
                            "headings_json": json.dumps(["Definition"]),
                            "source_ids_json": json.dumps([]),
                            "content_hash": "hash",
                            "rank": -1.0,
                        }
                    ]
                )

            def close(self):
                pass

        connection = FakeConnection()
        with mock.patch.object(QIDX.sqlite3, "connect", return_value=connection):
            results, indexed = QIDX.query_fts_index(
                Path("query-index.sqlite3"),
                "scaling",
                "all",
                QIDX.MAX_QUERY_LIMIT + 500,
            )

        self.assertEqual(1, indexed)
        self.assertEqual(["wiki/concepts/scaling-laws.md"], [result["path"] for result in results])
        select_sql, select_params = connection.executed[1]
        self.assertIn("ORDER BY", select_sql)
        self.assertIn("LIMIT ?", select_sql)
        self.assertEqual(QIDX.MAX_QUERY_LIMIT, select_params[-1])

    def test_relative_custom_index_path_works_for_build_and_query(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            index_path = root / "custom-cache" / "query-index.sqlite3"

            code, output = self.build_index(root, "custom-cache/query-index.sqlite3")

            self.assertEqual(code, 0)
            self.assertTrue(index_path.is_file())
            self.assertIn("Indexed 3 documents", output)

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--index-path",
                    "custom-cache/query-index.sqlite3",
                    "--format",
                    "json",
                    "scaling laws",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertGreaterEqual(payload["result_count"], 1)
            self.assertEqual("scaling laws", payload["query"])

    def test_resolve_index_path_rejects_unsafe_workspace_relative_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            for unsafe in ("../evil.db", "/tmp/evil.db", "C:\\evil.db", "https://example.test/index.db"):
                with self.subTest(index_path=unsafe):
                    with self.assertRaises(SystemExit) as context:
                        QIDX.resolve_index_path(root, unsafe)

                    message = str(context.exception)
                    self.assertIn("index_path", message)
                    self.assertIn("workspace-relative", message)

    def test_fts_ranking_prefers_title_heading_match_over_body_only_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text(
                "wiki:\n  root: wiki\nsources:\n  normalized_dir: sources/normalized\n"
            )
            wiki_dir = root / "wiki" / "concepts"
            wiki_dir.mkdir(parents=True)
            (wiki_dir / "retrieval-generation.md").write_text(
                "---\ntype: concept\nsource_ids: []\n---\n\n"
                "# Retrieval Generation\n\n## Retrieval Generation\n\n"
                "A concise maintained note.\n"
            )
            normalized = root / "sources" / "normalized"
            normalized.mkdir(parents=True)
            (normalized / "body-only.md").write_text(
                "---\ntype: normalized_source\nsource_id: paper:body-only\n"
                "source_kind: paper\n---\n\n"
                "# Body Only\n\n"
                + ("retrieval generation " * 30)
            )

            self.build_index(root)
            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--format",
                    "json",
                    "retrieval generation",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["results"][0]["path"], "wiki/concepts/retrieval-generation.md")

    def test_fts_prefix_recall_matches_stemmed_query_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "research.yml").write_text(
                "wiki:\n  root: wiki\nsources:\n  normalized_dir: sources/normalized\n"
            )
            wiki_dir = root / "wiki" / "concepts"
            wiki_dir.mkdir(parents=True)
            (wiki_dir / "rag.md").write_text(
                "---\ntype: concept\nsource_ids: []\n---\n\n"
                "# Retrieval Augmented Generation\n\n"
                "Retrieval and generation are paired in this workflow.\n"
            )

            config = QIDX.load_config(root)
            documents = QIDX.build_index(root, config, "all")
            lexical_results = QIDX.rank_documents(documents, "retriev generat", 10)
            self.assertEqual(lexical_results, [])

            self.build_index(root)
            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--format",
                    "json",
                    "retriev generat",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["result_count"], 1)
            self.assertEqual(payload["results"][0]["path"], "wiki/concepts/rag.md")

    def test_fts_scope_filtering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.build_index(root)

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--scope",
                    "normalized",
                    "--format",
                    "json",
                    "scaling",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertGreater(payload["result_count"], 0)
            self.assertTrue(all(r["scope"] == "normalized" for r in payload["results"]))

    def test_corrupt_index_falls_back_to_lexical_query_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            index_path = root / ".research-cache" / "query-index.sqlite3"
            index_path.parent.mkdir()
            index_path.write_text("not a sqlite database")

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--index-path",
                    ".research-cache/query-index.sqlite3",
                    "--format",
                    "json",
                    "scaling laws",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertGreaterEqual(payload["result_count"], 1)
            self.assertEqual(payload["results"][0]["path"], "wiki/concepts/scaling-laws.md")

    def test_fts_exact_source_id_match_boost(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.build_index(root)

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--format",
                    "json",
                    "paper:scaling-2024",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["results"][0]["path"], "sources/normalized/paper--scaling-2024.md")

    def test_fts_results_include_related_source_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.add_citation_graph_records(root)
            self.build_index(root)

            code, output = self.run_cli(
                [
                    "--project-root",
                    str(root),
                    "--format",
                    "json",
                    "paper:scaling-2024",
                ]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            result = self.result_by_path(payload, "sources/normalized/paper--scaling-2024.md")
            self.assertEqual(
                ["paper:citing-2024", "paper:related-2024"],
                result["related_source_ids"],
            )

    def test_stale_index_falls_back_to_fresh_in_memory_results(self):
        # A persistent index must not silently serve outdated content after the
        # underlying wiki page changes without a rebuild.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.build_index(root)

            page = root / "wiki" / "concepts" / "scaling-laws.md"
            page.write_text(
                "---\ntype: concept\nsource_ids: []\n---\n\n"
                "# Diffusion Models\n\nThis page was rewritten and no longer mentions scaling.\n"
            )

            # "decreases" appeared only in the original wiki page body, not in
            # any normalized record, so a correct (non-stale) search returns 0.
            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "decreases"]
            )

            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["result_count"], 0)

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "diffusion"]
            )
            payload = json.loads(output)
            self.assertEqual(payload["results"][0]["path"], "wiki/concepts/scaling-laws.md")

    def test_added_file_is_visible_without_explicit_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.build_index(root)

            (root / "wiki" / "concepts" / "distillation.md").write_text(
                "---\ntype: concept\nsource_ids: []\n---\n\n"
                "# Knowledge Distillation\n\nDistillation transfers behavior to smaller models.\n"
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "distillation"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["result_count"], 1)
            self.assertEqual(payload["results"][0]["path"], "wiki/concepts/distillation.md")

    def test_narrow_scope_index_does_not_starve_wider_query(self):
        # An index built for one scope must not silently return empty/partial
        # results for a query that needs a scope it does not cover.
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            self.build_index(root, scope="wiki")

            code, output = self.run_cli(
                ["--project-root", str(root), "--scope", "normalized", "--format", "json", "scaling"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertGreater(payload["result_count"], 0)
            self.assertTrue(all(r["scope"] == "normalized" for r in payload["results"]))

    def test_fresh_scope_matched_index_is_used(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            index_path = root / ".research-cache" / "query-index.sqlite3"
            self.build_index(root)
            config = QIDX.load_config(root)
            usable, note = QIDX.evaluate_index(root, config, index_path, "all")
            self.assertTrue(usable)
            self.assertIsNone(note)

    def test_competing_index_builders_serialize_and_leave_no_temp_database(self):
        if not QIDX.sqlite_fts5_available():
            self.skipTest("SQLite FTS5 is unavailable on this interpreter")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            concepts = root / "wiki" / "concepts"
            for index in range(100):
                (concepts / f"concurrent-{index:03d}.md").write_text(
                    f"# Concurrent {index}\n\nShared index build fixture {index}.\n",
                    encoding="utf-8",
                )
            command = [
                sys.executable,
                str(SCRIPT_PATH),
                "build-index",
                "--project-root",
                str(root),
            ]
            builders = [
                subprocess.Popen(
                    command,
                    cwd=REPO_ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            results = [builder.communicate(timeout=20) for builder in builders]

            for builder, (stdout, stderr) in zip(builders, results, strict=True):
                self.assertEqual(0, builder.returncode, stderr)
                self.assertIn("Indexed 103 documents", stdout)
            index_path = root / QIDX.DEFAULT_INDEX_PATH
            metadata = QIDX.index_metadata(index_path)
            self.assertEqual(QIDX.INDEX_SCHEMA_VERSION, metadata["schema_version"])
            self.assertEqual("103", metadata["document_count"])
            self.assertEqual([], list(index_path.parent.glob(f".{index_path.name}.*.tmp*")))

    def test_reports_unnormalized_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            # paper:scaling-2024 already has a normalized record; paper:unprocessed
            # does not; the image kind is not normalizable and must be ignored.
            (root / "sources" / "manifest.jsonl").write_text(
                '{"id":"paper:scaling-2024","kind":"paper"}\n'
                '{"id":"paper:unprocessed","kind":"paper"}\n'
                '{"id":"img:fig","kind":"image"}\n'
            )

            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["unnormalized_source_count"], 1)
            self.assertEqual(payload["unnormalized_source_ids"], ["paper:unprocessed"])

            _code, text = self.run_cli(["--project-root", str(root), "scaling"])
            self.assertIn("not yet normalized", text)

    def test_no_manifest_reports_zero_unnormalized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            code, output = self.run_cli(
                ["--project-root", str(root), "--format", "json", "scaling"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(payload["unnormalized_source_count"], 0)
            _code, text = self.run_cli(["--project-root", str(root), "scaling"])
            self.assertNotIn("not yet normalized", text)

    def test_build_index_requires_fts5_support(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self.build_workspace(Path(tmpdir))
            with mock.patch.object(QIDX, "sqlite_fts5_available", return_value=False):
                with self.assertRaises(SystemExit) as error:
                    QIDX.main(["build-index", "--project-root", str(root)])
            self.assertIn("SQLite FTS5 is required", str(error.exception))


if __name__ == "__main__":
    unittest.main()
