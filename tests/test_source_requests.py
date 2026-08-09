import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
PROFILE_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "workspace-init-profile.yml"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REQUESTS = load_script_module("research_source_requests", "source_requests.py")
LOCKS = load_script_module("research_source_requests_locks", "_workspace_locks.py")
INIT = load_script_module("research_source_requests_init", "init_research_workspace.py")
STATUS = load_script_module("research_source_requests_status", "workspace_status.py")
ERRORS = load_script_module("research_source_requests_errors", "_script_errors.py")


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["script", *args]
    try:
        yield
    finally:
        sys.argv = old


class SourceRequestsTests(unittest.TestCase):
    def init_workspace(self, root: Path, questions: list[dict] | None = None) -> Path:
        target = root / "requests-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = questions or [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"}
        ]
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def run_requests(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = REQUESTS.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def requests_json(self, target: Path, *args: str) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_requests("--project-root", str(target), *args, "--format", "json")
        payload = json.loads(stdout) if stdout.strip() else {}
        return code, payload, stderr

    def add_request(self, target: Path, *extra: str, query: str = "arXiv:2601.00001") -> dict:
        code, payload, stderr = self.requests_json(
            target,
            "add",
            "--kind",
            "paper",
            "--query-or-identifier",
            query,
            "--rationale",
            "Blocks the benchmark question.",
            "--priority",
            "high",
            *extra,
        )
        self.assertEqual(0, code, stderr)
        return payload

    def artifact_lines(self, target: Path) -> list[dict]:
        path = target / "sources" / "source-requests.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def write_selected_candidate(self, target: Path, candidate: dict) -> None:
        path = target / "sources" / "discovery" / "candidates.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidate, sort_keys=True) + "\n", encoding="utf-8")

    def set_question_status(self, target: Path, slug: str, status: str, extra_fields: dict | None = None) -> None:
        page = target / "wiki" / "questions" / f"{slug}.md"
        text = page.read_text()
        replacement = f"status: {status}"
        for field, value in (extra_fields or {}).items():
            replacement += f"\n{field}: {value}"
        page.write_text(text.replace("status: open", replacement, 1))

    def deliver_and_inventory(self, target: Path) -> str:
        """Deliver one raw file, run inventory, and return its manifest id."""
        (target / "raw" / "papers" / "delivered-report.md").write_text("# Delivered Report\n\nEvidence.\n")
        inventory = load_script_module("research_source_requests_inventory", "source_inventory.py")
        with patched_argv("--project-root", str(target)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, inventory.main())
        manifest = target / "sources" / "manifest.jsonl"
        records = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        markdown = [record for record in records if record["kind"] == "markdown"]
        self.assertEqual(1, len(markdown))
        return markdown[0]["id"]

    def declare_pack_kind(self, target: Path) -> str:
        """Give the workspace a domain pack that declares one request kind."""
        config_path = target / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config["domain_pack"] = {
            "name": "market-data",
            "request_kinds": [
                {
                    "id": "pack:market-data/supplier_quote",
                    "label": "Supplier quote",
                    "description": "Live SKU price + shipping + MOQ from a named supplier.",
                }
            ],
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return "pack:market-data/supplier_quote"

    def deliver_scoped_source(self, target: Path, name: str, scope: dict | None = None) -> str:
        """Deliver one raw file, optionally scope-stamped, and return its manifest id."""
        relative = f"raw/papers/{name}.md"
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n\nEvidence.\n", encoding="utf-8")
        if scope is not None:
            sidecar = {"retrieved_at": "2026-06-10T12:00:00Z", "scope": scope}
            (target / f"{relative}.provenance.yml").write_text(
                yaml.safe_dump(sidecar, sort_keys=False), encoding="utf-8"
            )
        inventory = load_script_module("research_source_requests_inventory", "source_inventory.py")
        with patched_argv("--project-root", str(target)):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, inventory.main())
        manifest = target / "sources" / "manifest.jsonl"
        records = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
        matching = [record for record in records if relative in (record.get("raw_paths") or [])]
        self.assertEqual(1, len(matching), records)
        return matching[0]["id"]

    def error_envelope(self, target: Path, *args: str) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_requests("--project-root", str(target), *args, "--format", "json")
        self.assertEqual("", stdout)
        return code, json.loads(stderr), stderr

    def test_add_records_request_and_log_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            payload = self.add_request(target, "--question-slug", "which-benchmarks")

            self.assertTrue(payload["created"])
            record = payload["request"]
            self.assertEqual("1.0", record["schema_version"])
            self.assertTrue(record["request_id"].startswith("req-"))
            self.assertEqual("paper", record["kind"])
            self.assertEqual("open", record["status"])
            self.assertEqual(["which-benchmarks"], record["question_slugs"])
            self.assertIsNone(record["source_id"])

            lines = self.artifact_lines(target)
            self.assertEqual(1, len(lines))
            self.assertEqual(record["request_id"], lines[0]["request_id"])
            self.assertIn("source-request | Recorded source request", (target / "log.md").read_text())

    def test_add_duplicate_open_request_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target)

            duplicate = self.add_request(target, query="  arXiv:2601.00001  ")

            self.assertFalse(duplicate["created"])
            self.assertEqual(first["request"]["request_id"], duplicate["duplicate_of"])
            self.assertEqual(1, len(self.artifact_lines(target)))

    def test_list_orders_offset_times_by_utc_and_mutation_persists_canonical_z(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            path = target / "sources" / "source-requests.jsonl"
            records = [
                {
                    "schema_version": "1.0",
                    "request_id": "req-before-fallback",
                    "kind": "web",
                    "query_or_identifier": "https://example.org/before",
                    "status": "open",
                    "created_at": "2026-11-01T01:45:00-04:00",
                    "updated_at": "2026-11-01T01:45:00-04:00",
                },
                {
                    "schema_version": "1.0",
                    "request_id": "req-after-fallback",
                    "kind": "web",
                    "query_or_identifier": "https://example.org/after",
                    "status": "open",
                    "created_at": "2026-11-01T01:15:00-05:00",
                    "updated_at": "2026-11-01T01:15:00-05:00",
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            code, payload, stderr = self.requests_json(target, "list")

            self.assertEqual(0, code, stderr)
            self.assertEqual(
                ["req-before-fallback", "req-after-fallback"],
                [record["request_id"] for record in payload["requests"]],
            )
            self.assertEqual("2026-11-01T05:45:00Z", payload["requests"][0]["created_at"])
            self.assertEqual("2026-11-01T06:15:00Z", payload["requests"][1]["created_at"])

            REQUESTS.write_requests(path, REQUESTS.load_requests(path))
            persisted = self.artifact_lines(target)
            self.assertTrue(all(record["created_at"].endswith("Z") for record in persisted))

    def test_concurrent_adds_preserve_request_store_and_shared_log(self):
        if not LOCKS.multiprocess_lock_supported():
            self.skipTest("No process-safe workspace lock backend is available")
        count = 8
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPTS / "source_requests.py"),
                        "--project-root",
                        str(target),
                        "add",
                        "--kind",
                        "web",
                        "--query-or-identifier",
                        f"https://example.org/source-{index}",
                        "--rationale",
                        f"Concurrent source request {index}.",
                        "--priority",
                        "medium",
                        "--format",
                        "json",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(count)
            ]
            results = [process.communicate(timeout=30) for process in processes]

            self.assertEqual([0] * count, [process.returncode for process in processes], results)
            payloads = [json.loads(stdout) for stdout, _stderr in results]
            records = self.artifact_lines(target)
            log_text = (target / "log.md").read_text(encoding="utf-8")

        self.assertEqual(count, len(records))
        self.assertEqual(count, len({record["request_id"] for record in records}))
        self.assertEqual(
            {f"https://example.org/source-{index}" for index in range(count)},
            {record["query_or_identifier"] for record in records},
        )
        self.assertEqual(
            {payload["request"]["request_id"] for payload in payloads},
            {record["request_id"] for record in records},
        )
        self.assertEqual(count, log_text.count("source-request | Recorded source request"))
        for index in range(count):
            self.assertEqual(1, log_text.count(f"Needs: https://example.org/source-{index}\n"))

    def test_add_rejects_unknown_question_slug(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, _, stderr = self.run_requests(
                "--project-root",
                str(target),
                "add",
                "--kind",
                "paper",
                "--query-or-identifier",
                "arXiv:2601.00001",
                "--rationale",
                "Needed.",
                "--question-slug",
                "no-such-question",
            )

            self.assertEqual(2, code)
            self.assertIn("Unknown question slug", stderr)
            self.assertEqual([], self.artifact_lines(target))

    def test_list_filters_by_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.add_request(target)
            self.add_request(target, query="arXiv:2601.00002")

            code, payload, _ = self.requests_json(target, "list", "--status", "open")
            self.assertEqual(0, code)
            self.assertEqual("1.0", payload["schema_version"])
            self.assertEqual(2, payload["counts"]["total"])
            self.assertEqual(2, payload["counts"]["open"])
            self.assertEqual(2, len(payload["requests"]))

            code, payload, _ = self.requests_json(target, "list", "--status", "fulfilled")
            self.assertEqual(0, code)
            self.assertEqual([], payload["requests"])

    def test_fulfill_round_trip_with_delivered_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target)["request"]["request_id"]
            source_id = self.deliver_and_inventory(target)

            code, payload, stderr = self.requests_json(
                target, "fulfill", "--request-id", request_id, "--source-id", source_id
            )
            self.assertEqual(0, code, stderr)
            self.assertTrue(payload["updated"])
            self.assertEqual("fulfilled", payload["request"]["status"])
            self.assertEqual(source_id, payload["request"]["source_id"])

            lines = self.artifact_lines(target)
            self.assertEqual(1, len(lines))
            self.assertEqual("fulfilled", lines[0]["status"])
            self.assertIn("source-request | Fulfilled source request", (target / "log.md").read_text())

            # Re-fulfilling with the same source id is an idempotent no-op.
            code, payload, _ = self.requests_json(
                target, "fulfill", "--request-id", request_id, "--source-id", source_id
            )
            self.assertEqual(0, code)
            self.assertFalse(payload["updated"])

            # Relinking to a different source id is refused.
            code, _, stderr = self.run_requests(
                "--project-root", str(target), "fulfill", "--request-id", request_id, "--source-id", "paper:other"
            )
            self.assertEqual(2, code)
            self.assertIn("already fulfilled", stderr)

    def test_fulfill_rejects_unknown_request_and_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target)["request"]["request_id"]

            code, _, stderr = self.run_requests(
                "--project-root", str(target), "fulfill", "--request-id", "req-nope", "--source-id", "paper:x"
            )
            self.assertEqual(2, code)
            self.assertIn("Unknown request id", stderr)

            code, _, stderr = self.run_requests(
                "--project-root", str(target), "fulfill", "--request-id", request_id, "--source-id", "paper:x"
            )
            self.assertEqual(2, code)
            self.assertIn("Unknown source id", stderr)
            self.assertEqual("open", self.artifact_lines(target)[0]["status"])

    def test_plan_fetch_arxiv_request_suggests_download_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            created = self.add_request(target, "--question-slug", "which-benchmarks", query="arXiv:2601.00001v1")
            request_id = created["request"]["request_id"]
            artifact_before = (target / "sources" / "source-requests.jsonl").read_text()
            log_before = (target / "log.md").read_text()

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("plan-fetch", payload["action"])
            self.assertEqual("ready", payload["plan_status"])
            self.assertFalse(payload["network_io_executed"])
            self.assertEqual(request_id, payload["request"]["request_id"])
            self.assertFalse(payload["acquisition"]["enabled"])
            self.assertEqual(1, len(payload["routes"]))
            route = payload["routes"][0]
            self.assertEqual("arxiv", route["provider"])
            self.assertEqual("download-source", route["route"])
            self.assertEqual("high", route["confidence"])
            self.assertFalse(route["allowed_by_config"])
            self.assertEqual(
                [
                    "python3",
                    "scripts/fetch_sources.py",
                    "--format",
                    "json",
                    "arxiv",
                    "download",
                    "--id",
                    "2601.00001v1",
                    "--format",
                    "source",
                    "--request-id",
                    request_id,
                ],
                route["command_argv"],
            )
            self.assertIn("--request-id " + request_id, route["command"])
            self.assertEqual(
                [
                    {
                        "description": "paired arXiv PDF archival artifact",
                        "command": (
                            "python3 scripts/fetch_sources.py --format json arxiv download --id "
                            f"2601.00001v1 --format pdf --request-id {request_id}"
                        ),
                        "command_argv": [
                            "python3",
                            "scripts/fetch_sources.py",
                            "--format",
                            "json",
                            "arxiv",
                            "download",
                            "--id",
                            "2601.00001v1",
                            "--format",
                            "pdf",
                            "--request-id",
                            request_id,
                        ],
                    }
                ],
                route["companion_commands"],
            )
            self.assertTrue(any("Acquisition is disabled" in warning for warning in payload["warnings"]))
            self.assertEqual(artifact_before, (target / "sources" / "source-requests.jsonl").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_plan_fetch_unversioned_arxiv_request_suggests_id_search_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            created = self.add_request(target, "--question-slug", "which-benchmarks")
            request_id = created["request"]["request_id"]
            artifact_before = (target / "sources" / "source-requests.jsonl").read_text()
            log_before = (target / "log.md").read_text()

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("ready", payload["plan_status"])
            self.assertFalse(payload["network_io_executed"])
            self.assertEqual(1, len(payload["routes"]))
            route = payload["routes"][0]
            self.assertEqual("arxiv", route["provider"])
            self.assertEqual("search-by-id", route["route"])
            self.assertEqual("high", route["confidence"])
            self.assertFalse(route["allowed_by_config"])
            self.assertEqual(
                [
                    "python3",
                    "scripts/fetch_sources.py",
                    "--format",
                    "json",
                    "arxiv",
                    "search",
                    "--id-list",
                    "2601.00001",
                    "--max-results",
                    "5",
                ],
                route["command_argv"],
            )
            self.assertNotEqual("ambiguous", payload["plan_status"])
            self.assertEqual(artifact_before, (target / "sources" / "source-requests.jsonl").read_text())
            self.assertEqual(log_before, (target / "log.md").read_text())

    def test_plan_fetch_doi_request_suggests_openalex_get(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, query="https://doi.org/10.5555/example")["request"]["request_id"]

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("ready", payload["plan_status"])
            self.assertEqual(1, len(payload["routes"]))
            route = payload["routes"][0]
            self.assertEqual("openalex", route["provider"])
            self.assertEqual("get-by-doi", route["route"])
            self.assertEqual(
                [
                    "python3",
                    "scripts/fetch_sources.py",
                    "--format",
                    "json",
                    "openalex",
                    "get",
                    "--id-or-doi",
                    "10.5555/example",
                    "--output",
                    f"raw/papers/openalex-{request_id}-metadata.json",
                    "--request-id",
                    request_id,
                ],
                route["command_argv"],
            )
            self.assertIn("openalex download-pdf", route["reason"])

    def test_plan_fetch_selected_doi_candidates_use_distinct_metadata_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, query="Selected DOI candidates")["request"]["request_id"]
            candidates = [
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-url-doi",
                    "status": "selected",
                    "selected_for_request_id": request_id,
                    "url": "https://doi.org/10.5555/url-candidate",
                    "title": "URL DOI candidate",
                    "source_type": "supplemental_material",
                    "trust_tier": "publisher_primary",
                },
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-paper-doi",
                    "status": "selected",
                    "selected_for_request_id": request_id,
                    "url": "https://example.org/paper",
                    "title": "Paper DOI candidate",
                    "source_type": "paper",
                    "trust_tier": "publisher_primary",
                    "paper": {"doi": "10.5555/paper-candidate"},
                },
            ]
            store = target / "sources" / "discovery" / "candidates.jsonl"
            store.parent.mkdir(parents=True, exist_ok=True)
            store.write_text(
                "".join(json.dumps(candidate, sort_keys=True) + "\n" for candidate in candidates),
                encoding="utf-8",
            )

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

        self.assertEqual(0, code, stderr)
        self.assertEqual(2, payload["selected_candidate_count"])
        routes = {route["candidate_id"]: route for route in payload["candidate_routes"]}
        outputs = {
            candidate_id: route["command_argv"][route["command_argv"].index("--output") + 1]
            for candidate_id, route in routes.items()
        }
        self.assertEqual(
            f"raw/papers/openalex-{request_id}-cand-url-doi-metadata.json",
            outputs["cand-url-doi"],
        )
        self.assertEqual(
            f"raw/papers/openalex-{request_id}-cand-paper-doi-metadata.json",
            outputs["cand-paper-doi"],
        )
        self.assertEqual(2, len(set(outputs.values())))

    def test_plan_fetch_ambiguous_paper_query_suggests_candidate_routes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, query="Synthetic retrieval benchmark survey")["request"]["request_id"]

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("ambiguous", payload["plan_status"])
            self.assertEqual(["arxiv", "openalex"], [route["provider"] for route in payload["routes"]])
            self.assertEqual(["search", "resolve"], [route["route"] for route in payload["routes"]])
            self.assertTrue(all(route["confidence"] == "medium" for route in payload["routes"]))
            self.assertTrue(all(not route["allowed_by_config"] for route in payload["routes"]))

    def test_plan_fetch_selected_official_web_candidate_uses_web_provider(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config["integrations"]["acquisition"] = {
                "enabled": True,
                "providers": ["web"],
                "target_root": "raw/papers",
                "max_downloads_per_run": 10,
                "require_license_check": True,
                "web": {
                    "target_root": "raw/web",
                    "allowed_domains": ["seg-social.example"],
                    "max_download_bytes": 1024,
                },
            }
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
            request_id = self.add_request(
                target,
                "--kind",
                "web",
                query="current reduced fee official source",
            )["request"]["request_id"]
            self.write_selected_candidate(
                target,
                {
                    "schema_version": "1.0",
                    "candidate_id": "cand-official-fee",
                    "status": "selected",
                    "selected_for_request_id": request_id,
                    "url": "https://seg-social.example/fee",
                    "title": "Official fee guidance",
                    "source_type": "official_legal",
                    "trust_tier": "official_primary",
                    "official_source": True,
                    "recommended_action": "fetch",
                    "publisher": "Seguridad Social",
                    "jurisdiction": "ES",
                    "terms_url": "https://seg-social.example/terms",
                    "evidence_areas": ["social_security_contributions", "current_legal_figure"],
                },
            )

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

        self.assertEqual(0, code, stderr)
        self.assertEqual("ready", payload["plan_status"])
        self.assertEqual(1, payload["selected_candidate_count"])
        self.assertEqual(1, len(payload["candidate_routes"]))
        route = payload["candidate_routes"][0]
        self.assertEqual("web", route["provider"])
        self.assertEqual("get", route["route"])
        self.assertTrue(route["provider_backed"])
        self.assertTrue(route["allowed_by_config"])
        self.assertIsNone(route["manual_delivery"])
        self.assertEqual("cand-official-fee", route["candidate_id"])
        self.assertEqual(
            [
                "python3",
                "scripts/fetch_sources.py",
                "--format",
                "json",
                "web",
                "get",
                "--url",
                "https://seg-social.example/fee",
                "--request-id",
                request_id,
                "--candidate-id",
                "cand-official-fee",
                "--source-type",
                "official_legal",
                "--publisher",
                "Seguridad Social",
                "--jurisdiction",
                "ES",
                "--terms-url",
                "https://seg-social.example/terms",
                "--evidence-area",
                "social_security_contributions",
                "--evidence-area",
                "current_legal_figure",
            ],
            route["command_argv"],
        )

    def test_plan_fetch_fulfilled_request_reports_no_routes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target)["request"]["request_id"]
            source_id = self.deliver_and_inventory(target)
            code, _, stderr = self.requests_json(target, "fulfill", "--request-id", request_id, "--source-id", source_id)
            self.assertEqual(0, code, stderr)

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("already_fulfilled", payload["plan_status"])
            self.assertEqual([], payload["routes"])
            self.assertTrue(any(source_id in warning for warning in payload["warnings"]))

    def test_plan_fetch_non_paper_request_reports_unsupported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            code, created, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                "dataset",
                "--query-or-identifier",
                "Benchmark table CSV",
                "--rationale",
                "Blocks the benchmark question.",
            )
            self.assertEqual(0, code, stderr)
            request_id = created["request"]["request_id"]

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("unsupported", payload["plan_status"])
            self.assertEqual([], payload["routes"])
            self.assertTrue(any("manual delivery" in warning for warning in payload["warnings"]))

    def test_plan_fetch_unknown_request_uses_json_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, stdout, stderr = self.run_requests(
                "--project-root",
                str(target),
                "plan-fetch",
                "--request-id",
                "req-nope",
                "--format",
                "json",
            )

            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("REQUEST_UNKNOWN", envelope["error_code"])

    # --- CR-4: pack-declared request kinds ------------------------------------

    def test_add_accepts_declared_pack_kind_and_carries_it_verbatim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            kind = self.declare_pack_kind(target)

            code, payload, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                kind,
                "--query-or-identifier",
                "ACME-1 unit price from Globex",
                "--rationale",
                "Blocks the supplier-quote facet.",
            )

            self.assertEqual(0, code, stderr)
            self.assertEqual(kind, payload["request"]["kind"])
            self.assertEqual(kind, self.artifact_lines(target)[0]["kind"])

            code, listing, stderr = self.requests_json(target, "list")
            self.assertEqual(0, code, stderr)
            self.assertEqual([kind], [record["kind"] for record in listing["requests"]])

    def test_add_refuses_undeclared_pack_kind_with_stable_error_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.declare_pack_kind(target)

            code, envelope, _ = self.error_envelope(
                target,
                "add",
                "--kind",
                "pack:market-data/price_history",
                "--query-or-identifier",
                "ACME-1 price history",
                "--rationale",
                "Blocks the trend facet.",
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_KIND_UNDECLARED", envelope["error_code"])
            self.assertIn("pack:market-data/price_history", envelope["message"])
            self.assertEqual([], self.artifact_lines(target))

    def test_add_refuses_bare_pack_kind_and_names_the_prefixed_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.declare_pack_kind(target)

            code, envelope, _ = self.error_envelope(
                target,
                "add",
                "--kind",
                "market-data/supplier_quote",
                "--query-or-identifier",
                "ACME-1 unit price",
                "--rationale",
                "Blocks the supplier-quote facet.",
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_KIND_INVALID", envelope["error_code"])
            self.assertIn("pack:market-data/supplier_quote", envelope["message"])
            self.assertEqual([], self.artifact_lines(target))

    def test_add_accepts_every_builtin_kind_without_a_pack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.assertIn("structured_data", REQUESTS.REQUEST_KINDS)

            for index, kind in enumerate(REQUESTS.REQUEST_KINDS):
                code, payload, stderr = self.requests_json(
                    target,
                    "add",
                    "--kind",
                    kind,
                    "--query-or-identifier",
                    f"https://example.org/builtin-{index}",
                    "--rationale",
                    f"Built-in kind {kind}.",
                )
                self.assertEqual(0, code, stderr)
                self.assertEqual(kind, payload["request"]["kind"])

            self.assertEqual(
                list(REQUESTS.REQUEST_KINDS),
                [record["kind"] for record in self.artifact_lines(target)],
            )

    # --- CR-4: structured request scope ---------------------------------------

    def test_add_stores_scope_pairs_and_refuses_malformed_ones(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            payload = self.add_request(
                target,
                "--scope",
                "facet_id=supplier_quote",
                "--scope",
                "candidate=acme-widget",
            )
            self.assertEqual(
                {"facet_id": "supplier_quote", "candidate": "acme-widget"},
                payload["request"]["scope"],
            )
            self.assertEqual(
                {"facet_id": "supplier_quote", "candidate": "acme-widget"},
                self.artifact_lines(target)[0]["scope"],
            )

            malformed = [
                ["facet_id"],
                ["=acme-widget"],
                ["facet_id="],
                ["Facet_Id=acme-widget"],
                ["facet_id=a", "facet_id=b"],
            ]
            for pairs in malformed:
                scope_args: list[str] = []
                for pair in pairs:
                    scope_args.extend(["--scope", pair])
                code, envelope, _ = self.error_envelope(
                    target,
                    "add",
                    "--kind",
                    "paper",
                    "--query-or-identifier",
                    "arXiv:2601.00002",
                    "--rationale",
                    "Malformed scope.",
                    *scope_args,
                )
                self.assertEqual(2, code, pairs)
                self.assertEqual("REQUEST_SCOPE_INVALID", envelope["error_code"], pairs)

            self.assertEqual(1, len(self.artifact_lines(target)))

    def test_add_without_scope_omits_the_field_entirely(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            payload = self.add_request(target)

            self.assertNotIn("scope", payload["request"])
            self.assertNotIn("scope", self.artifact_lines(target)[0])

    def test_add_duplicate_detection_requires_equal_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target, "--scope", "candidate=acme-widget")

            same_scope = self.add_request(target, "--scope", "candidate=acme-widget")
            self.assertFalse(same_scope["created"])
            self.assertEqual(first["request"]["request_id"], same_scope["duplicate_of"])
            self.assertEqual(1, len(self.artifact_lines(target)))

            other_scope = self.add_request(target, "--scope", "candidate=globex-widget")
            self.assertTrue(other_scope["created"])
            self.assertNotEqual(first["request"]["request_id"], other_scope["request"]["request_id"])
            self.assertEqual(2, len(self.artifact_lines(target)))

            unscoped = self.add_request(target)
            self.assertTrue(unscoped["created"])
            self.assertEqual(3, len(self.artifact_lines(target)))

    def test_fulfill_accepts_source_whose_provenance_scope_agrees(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            lenient = self.add_request(target, "--scope", "facet_id=supplier_quote")["request"]["request_id"]
            strict = self.add_request(
                target,
                "--scope",
                "facet_id=supplier_quote",
                query="arXiv:2601.00002",
            )["request"]["request_id"]
            source_id = self.deliver_scoped_source(
                target,
                "agreeing-quote",
                {"facet_id": "supplier_quote", "candidate": "acme-widget"},
            )

            code, payload, stderr = self.requests_json(
                target, "fulfill", "--request-id", lenient, "--source-id", source_id
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("fulfilled", payload["request"]["status"])

            # All three layers together: agreeing keys, a caller assertion that matches both
            # sides, and every request key present in the delivery.
            code, payload, stderr = self.requests_json(
                target,
                "fulfill",
                "--request-id",
                strict,
                "--source-id",
                source_id,
                "--match-scope",
                "candidate=acme-widget",
                "--require-scope",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("fulfilled", payload["request"]["status"])

    def test_fulfill_refuses_contradicting_facet_scope_without_any_flag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, "--scope", "facet_id=supplier_quote")["request"]["request_id"]
            source_id = self.deliver_scoped_source(
                target, "wrong-facet", {"facet_id": "price_history"}
            )

            code, envelope, _ = self.error_envelope(
                target, "fulfill", "--request-id", request_id, "--source-id", source_id
            )

            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_MISMATCH", envelope["error_code"])
            self.assertIn("facet_id", envelope["message"])
            self.assertEqual(
                [{"key": "facet_id", "request_value": "supplier_quote", "source_value": "price_history"}],
                envelope["details"]["conflicts"],
            )
            # A mis-pairing is not a syntax error: the envelope must carry the remediation
            # registered for this code, not the scope parser's key=value advice.
            self.assertEqual(ERRORS.remediation_for("REQUEST_SCOPE_MISMATCH"), envelope["remediation"])
            record = self.artifact_lines(target)[0]
            self.assertEqual("open", record["status"])
            self.assertIsNone(record["source_id"])
            self.assertNotIn("Fulfilled source request", (target / "log.md").read_text())

    def test_fulfill_unstamped_source_passes_until_require_scope_is_asked_for(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, "--scope", "facet_id=supplier_quote")["request"]["request_id"]
            source_id = self.deliver_scoped_source(target, "unstamped-delivery")

            code, envelope, _ = self.error_envelope(
                target,
                "fulfill",
                "--request-id",
                request_id,
                "--source-id",
                source_id,
                "--require-scope",
            )
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_MISSING", envelope["error_code"])
            self.assertIn("facet_id", envelope["message"])
            self.assertEqual(["facet_id"], envelope["details"]["missing_keys"])
            self.assertEqual(ERRORS.remediation_for("REQUEST_SCOPE_MISSING"), envelope["remediation"])
            self.assertEqual("open", self.artifact_lines(target)[0]["status"])

            code, payload, stderr = self.requests_json(
                target, "fulfill", "--request-id", request_id, "--source-id", source_id
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("fulfilled", payload["request"]["status"])

    def test_fulfill_refuses_match_scope_that_contradicts_the_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, "--scope", "facet_id=supplier_quote")["request"]["request_id"]
            source_id = self.deliver_scoped_source(
                target, "matching-quote", {"facet_id": "supplier_quote"}
            )

            code, envelope, _ = self.error_envelope(
                target,
                "fulfill",
                "--request-id",
                request_id,
                "--source-id",
                source_id,
                "--match-scope",
                "facet_id=price_history",
            )
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_MISMATCH", envelope["error_code"])
            self.assertEqual("--match-scope", envelope["details"]["option"])
            self.assertEqual("open", self.artifact_lines(target)[0]["status"])

            code, envelope, _ = self.error_envelope(
                target,
                "fulfill",
                "--request-id",
                request_id,
                "--source-id",
                source_id,
                "--match-scope",
                "facet_id",
            )
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_INVALID", envelope["error_code"])
            self.assertEqual("open", self.artifact_lines(target)[0]["status"])

            code, payload, stderr = self.requests_json(
                target,
                "fulfill",
                "--request-id",
                request_id,
                "--source-id",
                source_id,
                "--match-scope",
                "facet_id=supplier_quote",
            )
            self.assertEqual(0, code, stderr)
            self.assertEqual("fulfilled", payload["request"]["status"])

    # --- CR-4: read-surface filters -------------------------------------------

    def test_list_filters_by_kind_and_by_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            pack_kind = self.declare_pack_kind(target)
            paper_id = self.add_request(
                target, "--scope", "facet_id=benchmarks", "--scope", "candidate=acme-widget"
            )["request"]["request_id"]
            code, created, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                pack_kind,
                "--query-or-identifier",
                "ACME-1 unit price",
                "--rationale",
                "Blocks the supplier-quote facet.",
                "--scope",
                "facet_id=supplier_quote",
                "--scope",
                "candidate=acme-widget",
            )
            self.assertEqual(0, code, stderr)
            quote_id = created["request"]["request_id"]
            code, created, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                "web",
                "--query-or-identifier",
                "https://example.org/unscoped",
                "--rationale",
                "No scope declared.",
            )
            self.assertEqual(0, code, stderr)
            web_id = created["request"]["request_id"]

            code, payload, stderr = self.requests_json(target, "list", "--kind", "paper")
            self.assertEqual(0, code, stderr)
            self.assertEqual(["paper"], payload["filter_kinds"])
            self.assertEqual([paper_id], [record["request_id"] for record in payload["requests"]])
            self.assertEqual(3, payload["counts"]["total"])

            code, payload, _ = self.requests_json(target, "list", "--kind", pack_kind, "--kind", "web")
            self.assertEqual(0, code)
            self.assertEqual({quote_id, web_id}, {record["request_id"] for record in payload["requests"]})

            code, payload, _ = self.requests_json(target, "list", "--scope", "candidate=acme-widget")
            self.assertEqual(0, code)
            self.assertEqual({"candidate": "acme-widget"}, payload["filter_scope"])
            self.assertEqual({paper_id, quote_id}, {record["request_id"] for record in payload["requests"]})

            # AND over both pairs, exact equality.
            code, payload, _ = self.requests_json(
                target, "list", "--scope", "candidate=acme-widget", "--scope", "facet_id=supplier_quote"
            )
            self.assertEqual(0, code)
            self.assertEqual([quote_id], [record["request_id"] for record in payload["requests"]])

            code, payload, _ = self.requests_json(target, "list", "--scope", "candidate=nobody")
            self.assertEqual(0, code)
            self.assertEqual([], payload["requests"])

            # An unknown scope key is a filter nothing matches, not an error: keys are
            # workspace convention, so refusing one would make the read surface guess.
            code, payload, _ = self.requests_json(target, "list", "--scope", "no_such_key=anything")
            self.assertEqual(0, code)
            self.assertEqual([], payload["requests"])
            # Counts stay a whole-store census so "no match" reads differently from "empty".
            self.assertEqual(3, payload["counts"]["total"])

            code, payload, _ = self.requests_json(
                target, "list", "--kind", pack_kind, "--scope", "facet_id=benchmarks"
            )
            self.assertEqual(0, code)
            self.assertEqual([], payload["requests"])

    def test_list_text_output_prints_scope_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.add_request(target, "--scope", "facet_id=benchmarks", "--scope", "candidate=acme-widget")

            code, stdout, stderr = self.run_requests("--project-root", str(target), "list")

            self.assertEqual(0, code, stderr)
            self.assertIn("scope: candidate=acme-widget, facet_id=benchmarks", stdout)

    def test_list_refuses_unknown_kind_and_malformed_scope_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.declare_pack_kind(target)

            code, envelope, _ = self.error_envelope(
                target, "list", "--kind", "pack:market-data/price_history"
            )
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_KIND_UNDECLARED", envelope["error_code"])

            code, envelope, _ = self.error_envelope(target, "list", "--scope", "facet_id")
            self.assertEqual(2, code)
            self.assertEqual("REQUEST_SCOPE_INVALID", envelope["error_code"])

    def test_plan_fetch_pack_and_structured_kinds_land_on_manual_delivery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            pack_kind = self.declare_pack_kind(target)
            code, created, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                pack_kind,
                "--query-or-identifier",
                "ACME-1 unit price",
                "--rationale",
                "Blocks the supplier-quote facet.",
                "--scope",
                "facet_id=supplier_quote",
            )
            self.assertEqual(0, code, stderr)
            pack_request_id = created["request"]["request_id"]
            code, created, stderr = self.requests_json(
                target,
                "add",
                "--kind",
                "structured_data",
                "--query-or-identifier",
                "Quarterly price table JSON",
                "--rationale",
                "Blocks the trend facet.",
            )
            self.assertEqual(0, code, stderr)
            structured_request_id = created["request"]["request_id"]

            for request_id in (pack_request_id, structured_request_id):
                code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

                self.assertEqual(0, code, stderr)
                self.assertEqual("unsupported", payload["plan_status"])
                self.assertEqual([], payload["routes"])
                self.assertFalse(payload["network_io_executed"])
                self.assertIn(REQUESTS.UNSUPPORTED_KIND_WARNING, payload["warnings"])

    def test_workspace_status_surfaces_open_requests_in_blocked_verdict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, "--question-slug", "which-benchmarks")["request"]["request_id"]
            self.set_question_status(
                target,
                "which-benchmarks",
                "blocked",
                {
                    "blocked_reason": "Needs the benchmark report from a fetch agent.",
                    "blocking_request_ids": f"[{request_id}]",
                },
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = STATUS.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, code)
            document = json.loads(stdout.getvalue())

            self.assertEqual(1, document["sources"]["requests_open"])
            self.assertEqual([request_id], document["sources"]["requests_open_ids"])
            self.assertEqual("blocked_on_sources", document["readiness"]["verdict"])
            self.assertTrue(any(request_id in reason for reason in document["readiness"]["reasons"]))

    def test_workspace_status_flags_blocked_questions_without_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.set_question_status(
                target,
                "which-benchmarks",
                "blocked",
                {"blocked_reason": "Needs evidence nobody has requested yet."},
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = STATUS.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, code)
            document = json.loads(stdout.getvalue())

            self.assertEqual(0, document["sources"]["requests_open"])
            self.assertEqual("attention_required", document["readiness"]["verdict"])
            self.assertTrue(
                any("lack valid open source request links" in reason for reason in document["readiness"]["reasons"])
            )


if __name__ == "__main__":
    unittest.main()
