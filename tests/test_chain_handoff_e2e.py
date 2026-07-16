import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "chain-handoff"
INIT_SCRIPT_PATH = REPO_ROOT / "workspace-template" / "scripts" / "init_research_workspace.py"


def load_script_module(name: str, path: Path):
    loader_path = path.parent / "_workspace_module_loader.py"
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
        return loader_module.load_workspace_module(path.parent, path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def patched_argv(*args: str):
    old = sys.argv
    sys.argv = ["script", *args]
    try:
        yield
    finally:
        sys.argv = old


INIT = load_script_module("chain_handoff_init", INIT_SCRIPT_PATH)


def tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class ChainHandoffE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(FIXTURE_ROOT.is_dir(), "missing chain-handoff fixture")

    def deploy_workspace(self, root: Path) -> Path:
        target = root / "chain-handoff-workspace"
        profile = yaml.safe_load((FIXTURE_ROOT / "workspace-init.yml").read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile_path = root / "workspace-init.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            code = INIT.main(["--profile", str(profile_path)])
        self.assertEqual(0, code)
        return target

    def load_workspace_scripts(self, workspace: Path) -> dict[str, object]:
        scripts = workspace / "scripts"
        return {
            "locks": load_script_module("chain_handoff_locks", scripts / "_workspace_locks.py"),
            "inventory": load_script_module("chain_handoff_inventory", scripts / "source_inventory.py"),
            "normalize": load_script_module("chain_handoff_normalize", scripts / "normalize_sources.py"),
            "intake": load_script_module("chain_handoff_intake", scripts / "intake_questions.py"),
            "status": load_script_module("chain_handoff_status", scripts / "workspace_status.py"),
            "question_status": load_script_module("chain_handoff_question_status", scripts / "question_status.py"),
            "requests": load_script_module("chain_handoff_requests", scripts / "source_requests.py"),
            "run_report": load_script_module("chain_handoff_run_report", scripts / "run_report.py"),
            "export": load_script_module("chain_handoff_export", scripts / "export_answers.py"),
            "fetch": load_script_module("chain_handoff_fetch", scripts / "fetch_sources.py"),
            "claim": load_script_module("chain_handoff_claim", scripts / "question_claim.py"),
            "resolve": load_script_module("chain_handoff_resolve", scripts / "question_resolve.py"),
        }

    def run_script(self, module: object, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(args)
        return int(code or 0), stdout.getvalue(), stderr.getvalue()

    def run_json(self, module: object, args: list[str]) -> tuple[int, dict, str]:
        code, stdout, stderr = self.run_script(module, args)
        return code, json.loads(stdout), stderr

    def run_argv_script(self, module: object, *args: str) -> int:
        with patched_argv(*args):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return module.main()

    def status_json(self, scripts: dict[str, object], workspace: Path) -> tuple[int, dict]:
        code, payload, _ = self.run_json(
            scripts["status"],
            ["--project-root", str(workspace), "--check-complete", "--format", "json"],
        )
        return code, payload

    def copy_delivery(self, workspace: Path) -> None:
        delivery = FIXTURE_ROOT / "delivery" / "raw"
        for source in sorted(delivery.rglob("*")):
            if source.is_dir():
                continue
            target = workspace / source.relative_to(FIXTURE_ROOT / "delivery")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def manifest_records(self, workspace: Path) -> list[dict]:
        manifest = workspace / "sources" / "manifest.jsonl"
        return [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]

    def source_id_for_raw_path(self, records: list[dict], raw_path: str) -> str:
        for record in records:
            if raw_path in record.get("raw_paths", []):
                return record["id"]
        raise AssertionError(f"manifest has no record carrying raw path {raw_path!r}")

    def source_id_for_kind(self, records: list[dict], kind: str) -> str:
        matches = [record["id"] for record in records if record.get("kind") == kind]
        self.assertEqual(1, len(matches), f"expected one {kind} record")
        return matches[0]

    def source_id_for_request_id(self, records: list[dict], request_id: str) -> str:
        matches = [
            record["id"]
            for record in records
            if record.get("provenance", {}).get("request_id") == request_id
        ]
        self.assertEqual(1, len(matches), f"expected one source record for request {request_id}")
        return matches[0]

    def read_frontmatter(self, page: Path) -> tuple[dict, str]:
        text = page.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        end = text.find("\n---", 4)
        self.assertNotEqual(-1, end)
        body_start = text.find("\n", end + 1)
        body = text[body_start + 1 :] if body_start != -1 else ""
        return yaml.safe_load(text[4:end]), body

    def write_frontmatter(self, page: Path, frontmatter: dict, body: str) -> None:
        rendered = yaml.safe_dump(frontmatter, sort_keys=False).strip()
        page.write_text(f"---\n{rendered}\n---\n\n{body}", encoding="utf-8")

    def update_question(
        self,
        workspace: Path,
        slug: str,
        updates: dict,
        remove: tuple[str, ...] = (),
    ) -> None:
        page = workspace / "wiki" / "questions" / f"{slug}.md"
        frontmatter, body = self.read_frontmatter(page)
        for key in remove:
            frontmatter.pop(key, None)
        frontmatter.update(updates)
        self.write_frontmatter(page, frontmatter, body)

    def write_answer(self, workspace: Path, filename: str, summary: str, source_ids: list[str]) -> str:
        answer_dir = workspace / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        answer_path = answer_dir / filename
        rendered_source_ids = "".join(f"  - {source_id}\n" for source_id in source_ids)
        answer_path.write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-06-13\n"
            "updated: 2026-06-13\n"
            f"source_ids:\n{rendered_source_ids}"
            f"summary: {summary}\n"
            "---\n\n"
            f"# {summary}\n\n"
            f"{summary}\n",
            encoding="utf-8",
        )
        return f"../synthesis/{filename}"

    def question_by_slug(self, export: dict, slug: str) -> dict:
        for question in export["questions"]:
            if question["slug"] == slug:
                return question
        raise AssertionError(f"export did not include question {slug!r}")

    def capture_question_baseline(self, scripts: dict[str, object], workspace: Path, path: Path) -> None:
        code, payload, _ = self.run_json(
            scripts["question_status"],
            ["--project-root", str(workspace), "--format", "json"],
        )
        self.assertEqual(0, code)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def capture_run_baseline(self, scripts: dict[str, object], workspace: Path, path: Path) -> dict:
        code, payload, _ = self.run_json(
            scripts["run_report"],
            ["baseline", "--project-root", str(workspace), "--output", str(path), "--format", "json"],
        )
        self.assertEqual(0, code)
        self.assertTrue(path.is_file())
        self.assertEqual(str(path.resolve()), payload["baseline_path"])
        return json.loads(path.read_text(encoding="utf-8"))

    def enable_arxiv_acquisition(self, workspace: Path) -> None:
        config_path = workspace / "research.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config.setdefault("integrations", {})["acquisition"] = {
            "enabled": True,
            "providers": ["arxiv"],
            "target_root": "raw/papers",
            "max_downloads_per_run": 10,
            "require_license_check": True,
        }
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def install_arxiv_transport(self, fetch: object, payload: bytes) -> list[tuple[str, float]]:
        calls: list[tuple[str, float]] = []

        def transport(url: str, timeout: float) -> bytes:
            calls.append((url, timeout))
            return payload

        fetch.ARXIV_TRANSPORT = transport
        fetch.ARXIV_CLOCK = lambda: 0.0
        fetch.ARXIV_SLEEP = lambda _seconds: None
        fetch.ARXIV_LAST_REQUEST_AT = None
        return calls

    def run_claim_race(self, workspace: Path, slug: str, root: Path) -> list[dict]:
        claim_script = workspace / "scripts" / "question_claim.py"
        barrier_dir = root / "claim-race-barrier"
        barrier_dir.mkdir()
        child_code = textwrap.dedent(
            """
            import importlib.util
            import json
            import os
            import pathlib
            import sys
            import time

            script_path = pathlib.Path(sys.argv[1])
            project_root = pathlib.Path(sys.argv[2])
            slug = sys.argv[3]
            agent_id = sys.argv[4]
            barrier_dir = pathlib.Path(sys.argv[5])
            question_path = (project_root / "wiki" / "questions" / f"{slug}.md").resolve()
            started_marker = barrier_dir / f"{agent_id}.started"
            opened_marker = barrier_dir / f"{agent_id}.opened"
            release_marker = barrier_dir / "release"
            real_open = pathlib.Path.open
            started_marker.write_text(str(os.getpid()), encoding="utf-8")

            def patched_open(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                try:
                    if self.resolve() == question_path:
                        opened_marker.write_text(str(os.getpid()), encoding="utf-8")
                        deadline = time.monotonic() + 10
                        while not release_marker.exists():
                            if time.monotonic() > deadline:
                                raise TimeoutError("timed out waiting for claim race release marker")
                            time.sleep(0.01)
                except BaseException:
                    handle.close()
                    raise
                return handle

            pathlib.Path.open = patched_open

            spec = importlib.util.spec_from_file_location(f"claim_race_{agent_id.replace('-', '_')}", script_path)
            if spec is None or spec.loader is None:
                print(json.dumps({"error": f"cannot load {script_path}"}), file=sys.stderr)
                raise SystemExit(97)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            raise SystemExit(
                module.main(
                    [
                        "--project-root",
                        str(project_root),
                        "claim",
                        "--slug",
                        slug,
                        "--agent-id",
                        agent_id,
                        "--format",
                        "json",
                    ]
                )
            )
            """
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", child_code, str(claim_script), str(workspace), slug, agent_id, str(barrier_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for agent_id in ("agent-a", "agent-b")
        ]
        try:
            deadline = time.monotonic() + 10
            first_opened_at: float | None = None
            started_markers = [barrier_dir / f"{agent_id}.started" for agent_id in ("agent-a", "agent-b")]
            opened_markers = [barrier_dir / f"{agent_id}.opened" for agent_id in ("agent-a", "agent-b")]
            while True:
                started = [marker for marker in started_markers if marker.exists()]
                opened = [marker for marker in opened_markers if marker.exists()]
                if len(opened) == len(opened_markers):
                    break
                if len(started) == len(started_markers) and opened:
                    if first_opened_at is None:
                        first_opened_at = time.monotonic()
                    if time.monotonic() - first_opened_at >= 2:
                        break
                exited = [process.returncode for process in processes if process.poll() is not None]
                if exited:
                    raise AssertionError(f"claim subprocess exited before the race release point: {exited}")
                if time.monotonic() > deadline:
                    raise AssertionError("timed out waiting for claim race processes to open the question page")
                time.sleep(0.01)
            (barrier_dir / "release").write_text("go", encoding="utf-8")
            results = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                payload = json.loads(stdout or stderr)
                results.append({"returncode": process.returncode, "payload": payload, "stdout": stdout, "stderr": stderr})
            return results
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_golden_handoff_chain_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.deploy_workspace(root)
            scripts = self.load_workspace_scripts(workspace)

            code, status = self.status_json(scripts, workspace)
            self.assertEqual(1, code)
            self.assertEqual("in_progress", status["readiness"]["verdict"])
            self.assertEqual("chain-task-0042", status["project"]["handoff"]["task_id"])

            self.copy_delivery(workspace)
            self.assertEqual(
                0,
                self.run_argv_script(scripts["inventory"], "--project-root", str(workspace)),
            )
            self.assertEqual(
                0,
                self.run_argv_script(scripts["normalize"], "--project-root", str(workspace), "--all"),
            )

            records = self.manifest_records(workspace)
            paper_id = self.source_id_for_raw_path(records, "raw/pdf/2601.00002v1.pdf")
            html_id = self.source_id_for_kind(records, "html")
            markdown_id = self.source_id_for_kind(records, "markdown")
            self.assertTrue(markdown_id)
            paper_record = next(record for record in records if record["id"] == paper_id)
            self.assertEqual("paired", paper_record["pairing_status"])
            self.assertEqual("CC-BY-4.0", paper_record["provenance"]["license"])
            self.assertTrue(paper_record["provenance"]["checksum_verified"])

            code, intake, _ = self.run_json(
                scripts["intake"],
                [
                    "--project-root", str(workspace),
                    "--from-file", str(FIXTURE_ROOT / "mid-run-questions.yml"),
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual({"task_id": "chain-task-0042"}, intake["handoff"])
            self.assertEqual(1, intake["counts"]["created"])

            baseline = root / "question-baseline.json"
            self.capture_question_baseline(scripts, workspace, baseline)

            benchmark_answer = self.write_answer(
                workspace,
                "benchmark-evidence.md",
                "The synthetic benchmark paper records the stable handoff evidence.",
                [paper_id],
            )
            self.update_question(
                workspace,
                "benchmark-evidence",
                {"status": "answered", "answer_page": benchmark_answer},
            )
            html_answer = self.write_answer(
                workspace,
                "implementation-risk.md",
                "The HTML delivery lists the remaining integration risk.",
                [html_id],
            )
            self.update_question(
                workspace,
                "implementation-risk",
                {"status": "answered", "answer_page": html_answer},
            )
            self.update_question(
                workspace,
                "maintenance-cost-gap",
                {
                    "status": "blocked",
                    "blocked_reason": "Needs the maintenance cost memo from the fetch agent.",
                },
            )

            code, request_report, _ = self.run_json(
                scripts["requests"],
                [
                    "--project-root", str(workspace),
                    "add",
                    "--kind", "web",
                    "--query-or-identifier", "https://example.org/chain-handoff/maintenance-costs",
                    "--rationale", "Blocks the maintenance cost gap question.",
                    "--priority", "high",
                    "--question-slug", "maintenance-cost-gap",
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            request_id = request_report["request"]["request_id"]

            code, blocked_status = self.status_json(scripts, workspace)
            self.assertEqual(3, code)
            self.assertEqual("blocked_on_sources", blocked_status["readiness"]["verdict"])
            self.assertEqual([request_id], blocked_status["sources"]["requests_open_ids"])

            code, blocked_export, _ = self.run_json(
                scripts["export"],
                ["--project-root", str(workspace), "--format", "json"],
            )
            self.assertEqual(0, code)
            self.assertEqual("chain-task-0042", blocked_export["project"]["handoff"]["task_id"])
            benchmark = self.question_by_slug(blocked_export, "benchmark-evidence")
            self.assertEqual(
                "The synthetic benchmark paper records the stable handoff evidence.",
                benchmark["answer_summary"],
            )
            self.assertEqual(paper_id, benchmark["citations"][0]["source_id"])
            self.assertTrue(benchmark["citations"][0]["normalized_record"])
            self.assertEqual("https://example.org/chain-handoff/synthetic-benchmark", benchmark["citations"][0]["origin_url"])
            self.assertEqual("CC-BY-4.0", benchmark["citations"][0]["license"])
            blocked = self.question_by_slug(blocked_export, "maintenance-cost-gap")
            self.assertEqual("blocked", blocked["status"])
            self.assertEqual(
                "Needs the maintenance cost memo from the fetch agent.",
                blocked["blocked_reason"],
            )

            code, _, _ = self.run_json(
                scripts["requests"],
                [
                    "--project-root", str(workspace),
                    "fulfill",
                    "--request-id", request_id,
                    "--source-id", html_id,
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            maintenance_answer = self.write_answer(
                workspace,
                "maintenance-costs.md",
                "The maintenance memo records bounded costs for the fixture handoff.",
                [html_id],
            )
            self.update_question(
                workspace,
                "maintenance-cost-gap",
                {"status": "answered", "answer_page": maintenance_answer},
                remove=("blocked_reason",),
            )

            code, final_status = self.status_json(scripts, workspace)
            self.assertEqual(0, code)
            self.assertEqual("complete", final_status["readiness"]["verdict"])

            code, report, _ = self.run_json(
                scripts["run_report"],
                [
                    "--project-root", str(workspace),
                    "--baseline", str(baseline),
                    "--agent-id", "chain-e2e-agent",
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertTrue((workspace / report["report_path"]).is_file())
            touched = {entry["slug"]: entry for entry in report["questions"]["touched"]}
            self.assertEqual(
                {"benchmark-evidence", "implementation-risk", "maintenance-cost-gap"},
                set(touched),
            )
            self.assertIn(request_id, report["source_requests"]["opened"])
            self.assertIn(request_id, report["source_requests"]["fulfilled"])

            code, final_export, _ = self.run_json(
                scripts["export"],
                ["--project-root", str(workspace), "--format", "json"],
            )
            self.assertEqual(0, code)
            self.assertEqual({"answered": 3}, dict(final_export["counts"]["by_status"]))
            maintenance = self.question_by_slug(final_export, "maintenance-cost-gap")
            self.assertEqual(html_id, maintenance["citations"][0]["source_id"])
            self.assertTrue(maintenance["citations"][0]["normalized_record"])

    def test_hardening_handoff_chain_covers_repaired_surfaces(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.deploy_workspace(root)
            scripts = self.load_workspace_scripts(workspace)

            fetch_calls: list[tuple[str, float]] = []

            def disabled_transport(url: str, timeout: float) -> bytes:
                fetch_calls.append((url, timeout))
                raise AssertionError("disabled acquisition must not call transport")

            scripts["fetch"].ARXIV_TRANSPORT = disabled_transport
            code, stdout, stderr = self.run_script(
                scripts["fetch"],
                [
                    "--project-root", str(workspace),
                    "--format", "json",
                    "arxiv",
                    "search",
                    "--query", "chain handoff benchmark",
                    "--max-results", "1",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            envelope = json.loads(stderr)
            self.assertEqual("ACQUISITION_DISABLED", envelope["error_code"])
            self.assertIn("integrations.acquisition.enabled: true", envelope["remediation"])
            self.assertEqual([], fetch_calls)

            manifest_path = workspace / "sources" / "manifest.jsonl"
            if manifest_path.exists():
                manifest_path.unlink()
            code, stdout, stderr = self.run_script(
                scripts["normalize"],
                [
                    "--project-root", str(workspace),
                    "--source-id", "paper:missing-before-inventory",
                    "--format", "json",
                ],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("MANIFEST_MISSING", json.loads(stderr)["error_code"])

            missing_workspace = root / "missing-workspace"
            code, stdout, stderr = self.run_script(
                scripts["inventory"],
                ["--project-root", str(missing_workspace), "--format", "json", "--report"],
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout)
            self.assertEqual("CONFIG_MISSING", json.loads(stderr)["error_code"])

            baseline = root / "run-baseline.json"
            baseline_payload = self.capture_run_baseline(scripts, workspace, baseline)
            self.assertEqual("run_report_baseline", baseline_payload["document_type"])
            self.assertEqual([], baseline_payload["normalized_sources"])

            code, intake, _ = self.run_json(
                scripts["intake"],
                [
                    "--project-root", str(workspace),
                    "--from-file", str(FIXTURE_ROOT / "mid-run-questions.yml"),
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual(1, intake["counts"]["created"])

            code, request_report, _ = self.run_json(
                scripts["requests"],
                [
                    "--project-root", str(workspace),
                    "add",
                    "--kind", "paper",
                    "--query-or-identifier", "arXiv:2601.00003v1",
                    "--rationale", "Blocks the maintenance cost gap question.",
                    "--priority", "high",
                    "--question-slug", "maintenance-cost-gap",
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            request_id = request_report["request"]["request_id"]

            requests_before_plan = (workspace / "sources" / "source-requests.jsonl").read_text(encoding="utf-8")
            log_before_plan = (workspace / "log.md").read_text(encoding="utf-8")
            code, plan, _ = self.run_json(
                scripts["requests"],
                [
                    "--project-root", str(workspace),
                    "plan-fetch",
                    "--request-id", request_id,
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual("ready", plan["plan_status"])
            self.assertFalse(plan["network_io_executed"])
            self.assertFalse(plan["acquisition"]["enabled"])
            self.assertEqual("arxiv", plan["routes"][0]["provider"])
            self.assertFalse(plan["routes"][0]["allowed_by_config"])
            self.assertTrue(any("Acquisition is disabled" in warning for warning in plan["warnings"]))
            self.assertEqual(requests_before_plan, (workspace / "sources" / "source-requests.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(log_before_plan, (workspace / "log.md").read_text(encoding="utf-8"))

            self.enable_arxiv_acquisition(workspace)
            archive = tar_gz_bytes(
                {
                    "main.tex": (
                        b"\\documentclass{article}\n"
                        b"\\begin{document}\n"
                        b"Fetched source closes the hardening handoff chain.\n"
                        b"\\end{document}\n"
                    ),
                    "sections/results.tex": b"Deterministic fetched evidence.\n",
                }
            )
            fetch_calls = self.install_arxiv_transport(scripts["fetch"], archive)
            code, download, _ = self.run_json(
                scripts["fetch"],
                [
                    "--project-root", str(workspace),
                    "--format", "json",
                    "arxiv",
                    "download",
                    "--id", "2601.00003v1",
                    "--format", "source",
                    "--request-id", request_id,
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual("raw/papers/arxiv-2601.00003v1", download["target_path"])
            self.assertEqual(
                [
                    "https://export.arxiv.org/api/query?start=0&max_results=1&id_list=2601.00003v1",
                    "https://arxiv.org/e-print/2601.00003v1",
                ],
                [url for url, _timeout in fetch_calls],
            )

            self.copy_delivery(workspace)
            self.assertEqual(0, self.run_argv_script(scripts["inventory"], "--project-root", str(workspace)))
            self.assertEqual(
                0,
                self.run_argv_script(scripts["normalize"], "--project-root", str(workspace), "--all"),
            )
            records = self.manifest_records(workspace)
            fetched_source_id = self.source_id_for_request_id(records, request_id)
            self.assertTrue(fetched_source_id.startswith("paper:2601.00003v1"))

            code, _, _ = self.run_json(
                scripts["requests"],
                [
                    "--project-root", str(workspace),
                    "fulfill",
                    "--request-id", request_id,
                    "--source-id", fetched_source_id,
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)

            if not scripts["locks"].multiprocess_lock_supported():
                self.skipTest("No process-safe workspace lock backend is available")
            race_results = self.run_claim_race(workspace, "maintenance-cost-gap", root)
            self.assertEqual([0, 3], sorted(result["returncode"] for result in race_results), race_results)
            success = next(result for result in race_results if result["returncode"] == 0)
            conflict = next(result for result in race_results if result["returncode"] == 3)
            winner = success["payload"]["holder"]["claimed_by"]
            loser = "agent-b" if winner == "agent-a" else "agent-a"
            self.assertEqual("CLAIM_HELD", conflict["payload"]["error_code"])
            page = workspace / "wiki" / "questions" / "maintenance-cost-gap.md"
            before_wrong_resolution = page.read_text(encoding="utf-8")
            self.assertEqual(1, sum(1 for line in before_wrong_resolution.splitlines() if line.startswith("claimed_by:")))

            code, stdout, stderr = self.run_script(
                scripts["resolve"],
                [
                    "--project-root", str(workspace),
                    "block",
                    "--slug", "maintenance-cost-gap",
                    "--agent-id", loser,
                    "--blocked-reason", "Wrong agent should not resolve this question.",
                    "--request-id", request_id,
                    "--format", "json",
                ],
            )
            self.assertEqual(3, code)
            self.assertEqual("", stdout)
            self.assertEqual("CLAIM_HELD", json.loads(stderr)["error_code"])
            self.assertEqual(before_wrong_resolution, page.read_text(encoding="utf-8"))

            code, resolved, _ = self.run_json(
                scripts["resolve"],
                [
                    "--project-root", str(workspace),
                    "block",
                    "--slug", "maintenance-cost-gap",
                    "--agent-id", winner,
                    "--blocked-reason", "Fetched source still needs synthesis by the answer agent.",
                    "--request-id", request_id,
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertEqual("blocked", resolved["status"])
            frontmatter, _ = self.read_frontmatter(page)
            self.assertEqual("blocked", frontmatter["status"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("claimed_at", frontmatter)

            code, report, _ = self.run_json(
                scripts["run_report"],
                [
                    "--project-root", str(workspace),
                    "--baseline", str(baseline),
                    "--agent-id", "chain-hardening-agent",
                    "--format", "json",
                ],
            )
            self.assertEqual(0, code)
            self.assertIn(request_id, report["source_requests"]["opened"])
            self.assertIn(request_id, report["source_requests"]["fulfilled"])
            self.assertIn(fetched_source_id, report["sources_normalized"])
            self.assertEqual([], report["sources_normalized_legacy_date_match"])

if __name__ == "__main__":
    unittest.main()
