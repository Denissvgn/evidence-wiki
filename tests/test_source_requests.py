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

    def test_add_records_request_and_log_entry(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            first = self.add_request(target)

            duplicate = self.add_request(target, query="  arXiv:2601.00001  ")

            self.assertFalse(duplicate["created"])
            self.assertEqual(first["request"]["request_id"], duplicate["duplicate_of"])
            self.assertEqual(1, len(self.artifact_lines(target)))

    def test_list_orders_offset_times_by_utc_and_mutation_persists_canonical_z(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            request_id = self.add_request(target, query="https://doi.org/10.5555/example")["request"]["request_id"]

            code, payload, stderr = self.requests_json(target, "plan-fetch", "--request-id", request_id)

            self.assertEqual(0, code, stderr)
            self.assertEqual("ready", payload["plan_status"])
            self.assertEqual(1, len(payload["routes"]))
            route = payload["routes"][0]
            self.assertEqual("openalex", route["provider"])
            self.assertEqual("get-by-doi", route["route"])
            self.assertEqual("10.5555/example", route["command_argv"][-1])
            self.assertIn("openalex download-pdf", route["reason"])

    def test_plan_fetch_ambiguous_paper_query_suggests_candidate_routes(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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

    def test_workspace_status_surfaces_open_requests_in_blocked_verdict(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
        with tempfile.TemporaryDirectory(dir="/tmp") as tmpdir:
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
