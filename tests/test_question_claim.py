import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta, timezone
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


CLAIM = load_script_module("research_question_claim", "question_claim.py")
LOCKS = load_script_module("research_question_claim_locks", "_workspace_locks.py")
INIT = load_script_module("research_question_claim_init", "init_research_workspace.py")
QUESTION_STATUS = load_script_module("research_question_claim_status", "question_status.py")
LINT = load_script_module("research_question_claim_lint", "lint.py")
STATUS = load_script_module("research_question_claim_workspace_status", "workspace_status.py")
EXPORT = load_script_module("research_question_claim_export", "export_answers.py")


class ClaimTestBase(unittest.TestCase):
    def init_workspace(self, root: Path, questions: list[dict] | None = None, run_block: dict | None = None) -> Path:
        target = root / "claim-workspace"
        profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
        profile["workspace_init"]["target_path"] = str(target)
        profile["workspace_init"]["questions"] = questions or [
            {"id": "which-benchmarks", "question": "Which benchmarks matter?", "priority": "high"},
            {"id": "second-question", "question": "What about datasets?", "priority": "medium"},
        ]
        if run_block is not None:
            profile["workspace_init"]["run"] = run_block
        profile_path = root / "profile.yml"
        profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
        with contextlib.redirect_stdout(io.StringIO()):
            INIT.main(["--profile", str(profile_path)])
        return target

    def run_claim(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = CLAIM.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def claim_json(self, target: Path, *args: str) -> tuple[int, dict]:
        code, stdout, stderr = self.run_claim("--project-root", str(target), *args, "--format", "json")
        payload = json.loads(stdout or stderr)
        return code, payload

    def start_blocked_claim_processes(
        self,
        target: Path,
        slug: str,
        barrier_dir: Path,
        agent_ids: tuple[str, str] = ("agent-a", "agent-b"),
    ) -> list[tuple[str, subprocess.Popen]]:
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
        return [
            (
                agent_id,
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child_code,
                        str(SCRIPTS / "question_claim.py"),
                        str(target),
                        slug,
                        agent_id,
                        str(barrier_dir),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ),
            )
            for agent_id in agent_ids
        ]

    def wait_for_claim_race_release_point(
        self,
        barrier_dir: Path,
        processes: list[tuple[str, subprocess.Popen]],
        timeout: float = 10,
        serialized_grace: float = 2,
    ) -> None:
        deadline = time.monotonic() + timeout
        first_opened_at: float | None = None
        while time.monotonic() < deadline:
            started = [agent_id for agent_id, _ in processes if (barrier_dir / f"{agent_id}.started").exists()]
            opened = [agent_id for agent_id, _ in processes if (barrier_dir / f"{agent_id}.opened").exists()]
            if len(opened) == len(processes):
                return
            if len(started) == len(processes) and opened:
                if first_opened_at is None:
                    first_opened_at = time.monotonic()
                if time.monotonic() - first_opened_at >= serialized_grace:
                    return
            exited = [(agent_id, process.poll()) for agent_id, process in processes if process.poll() is not None]
            if exited:
                self.fail(f"claim subprocess exited before the race release point: {exited}")
            time.sleep(0.01)
        self.fail(
            "claim subprocesses did not reach the race release point before timeout: "
            f"started={started}, opened={opened}"
        )

    def parse_claim_process_payload(self, stdout: str, stderr: str) -> dict | None:
        for text in (stdout.strip(), stderr.strip()):
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        return None

    def collect_claim_process_results(
        self,
        processes: list[tuple[str, subprocess.Popen]],
        timeout: float = 10,
    ) -> list[dict]:
        results = []
        for agent_id, process in processes:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=2)
            results.append(
                {
                    "agent_id": agent_id,
                    "returncode": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "payload": self.parse_claim_process_payload(stdout, stderr),
                }
            )
        return results

    def stop_claim_processes(self, processes: list[tuple[str, subprocess.Popen]]) -> None:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def page_frontmatter(self, target: Path, slug: str) -> dict:
        text = (target / "wiki" / "questions" / f"{slug}.md").read_text()
        block = text.split("---\n", 2)[1]
        return yaml.safe_load(block)

    def set_claimed_at(self, target: Path, slug: str, hours_ago: float) -> None:
        page = target / "wiki" / "questions" / f"{slug}.md"
        old = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = page.read_text().split("\n")
        lines = [f'claimed_at: "{old}"' if line.startswith("claimed_at:") else line for line in lines]
        page.write_text("\n".join(lines))


class QuestionClaimTests(ClaimTestBase):
    """E20-T01: claim/release transitions and refusals."""

    def test_claim_transitions_open_to_in_progress_with_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, payload = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["applied"])
            self.assertEqual("claimed", payload["outcome"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("in_progress", frontmatter["status"])
            self.assertEqual("agent-a", frontmatter["claimed_by"])
            self.assertTrue(str(frontmatter["claimed_at"]).startswith("20"))
            self.assertIn("claim | Question claim", (target / "log.md").read_text())

    def test_two_agents_cannot_both_hold_a_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            code, _ = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")
            self.assertEqual(0, code)

            code, refusal = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-b")

            self.assertEqual(3, code)
            self.assertEqual("CLAIM_HELD", refusal["error_code"])
            self.assertFalse(refusal["recoverable"])
            self.assertIn("Use claim --steal --if-older-than", refusal["remediation"])
            self.assertEqual(
                {"action": "claim", "slug": "which-benchmarks", "agent_id": "agent-b"},
                refusal["details"],
            )
            self.assertIn("agent-a", refusal["message"])
            self.assertEqual("agent-a", self.page_frontmatter(target, "which-benchmarks")["claimed_by"])

    def test_competing_processes_do_not_both_report_claim_success(self):
        if not LOCKS.multiprocess_lock_supported():
            self.skipTest("No process-safe workspace lock backend is available")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = self.init_workspace(root)
            barrier_dir = root / "claim-race-barrier"
            barrier_dir.mkdir()
            processes = self.start_blocked_claim_processes(target, "which-benchmarks", barrier_dir)
            try:
                self.wait_for_claim_race_release_point(barrier_dir, processes)
                (barrier_dir / "release").write_text("go", encoding="utf-8")
                results = self.collect_claim_process_results(processes)
            finally:
                self.stop_claim_processes(processes)

            self.assertEqual([0, 3], sorted(result["returncode"] for result in results), results)
            successes = [result for result in results if result["returncode"] == 0]
            conflicts = [result for result in results if result["returncode"] == 3]
            self.assertEqual(1, len(successes), results)
            self.assertEqual(1, len(conflicts), results)
            self.assertEqual("CLAIM_HELD", conflicts[0]["payload"]["error_code"])

            page = target / "wiki" / "questions" / "which-benchmarks.md"
            text = page.read_text()
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("in_progress", frontmatter["status"])
            self.assertEqual(1, sum(1 for line in text.splitlines() if line.startswith("claimed_by:")))
            self.assertEqual(successes[0]["payload"]["holder"]["claimed_by"], frontmatter["claimed_by"])

    def test_claim_lock_file_is_stable_and_excluded_from_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            code, _ = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            self.assertEqual(0, code)
            locks_dir = target / "wiki" / "questions" / ".locks"
            self.assertTrue((locks_dir / "which-benchmarks.lock").is_file())

            (locks_dir / "ignored.md").write_text(
                "---\ntype: question\nstatus: open\n---\n# Runtime lock artifact\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status_code = QUESTION_STATUS.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, status_code)
            status_payload = json.loads(stdout.getvalue())
            self.assertEqual(2, status_payload["total"])
            self.assertNotIn("ignored", {record["slug"] for record in status_payload["questions"]})

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                export_code = EXPORT.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, export_code)
            export_payload = json.loads(stdout.getvalue())
            self.assertEqual(2, export_payload["counts"]["total"])
            self.assertNotIn("ignored", {record["slug"] for record in export_payload["questions"]})

            results = LINT.run_checks(target, LINT.load_config(target))
            expected_pages = [
                path
                for path in (target / "wiki").rglob("*.md")
                if ".locks" not in path.relative_to(target / "wiki").parts
            ]
            self.assertEqual(len(expected_pages), results["pages_checked"])
            self.assertFalse(
                any(".locks" in file for issue in results["issues"] for file in issue.get("files", []))
            )

    def test_same_agent_reclaim_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            code, payload = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            self.assertEqual(0, code)
            self.assertFalse(payload["applied"])
            self.assertEqual("already_claimed_by_agent", payload["outcome"])

    def test_release_reverts_to_open_and_clears_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            code, payload = self.claim_json(target, "release", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            self.assertEqual(0, code)
            self.assertEqual("released", payload["outcome"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("open", frontmatter["status"])
            self.assertNotIn("claimed_by", frontmatter)
            self.assertNotIn("claimed_at", frontmatter)

    def test_release_of_another_agents_claim_is_refused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            code, refusal = self.claim_json(target, "release", "--slug", "which-benchmarks", "--agent-id", "agent-b")

            self.assertEqual(3, code)
            self.assertEqual("CLAIM_HELD", refusal["error_code"])
            self.assertEqual("in_progress", self.page_frontmatter(target, "which-benchmarks")["status"])

    def test_steal_requires_threshold_and_respects_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            code, refusal = self.claim_json(
                target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-b", "--steal"
            )
            self.assertEqual(2, code)
            self.assertEqual("STEAL_THRESHOLD_REQUIRED", refusal["error_code"])

            code, refusal = self.claim_json(
                target,
                "claim", "--slug", "which-benchmarks", "--agent-id", "agent-b",
                "--steal", "--if-older-than", "1",
            )
            self.assertEqual(3, code)
            self.assertEqual("CLAIM_NOT_STALE", refusal["error_code"])

    def test_steal_transfers_stale_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")
            self.set_claimed_at(target, "which-benchmarks", hours_ago=30)

            code, payload = self.claim_json(
                target,
                "claim", "--slug", "which-benchmarks", "--agent-id", "agent-b",
                "--steal", "--if-older-than", "24",
            )

            self.assertEqual(0, code)
            self.assertEqual("stolen", payload["outcome"])
            self.assertEqual("agent-a", payload["previous_holder"]["claimed_by"])
            frontmatter = self.page_frontmatter(target, "which-benchmarks")
            self.assertEqual("agent-b", frontmatter["claimed_by"])
            self.assertEqual("in_progress", frontmatter["status"])

    def test_claim_refuses_unclaimable_status_and_unknown_slug(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            page = target / "wiki" / "questions" / "which-benchmarks.md"
            page.write_text(page.read_text().replace("status: open", "status: answered", 1))

            code, refusal = self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")
            self.assertEqual(2, code)
            self.assertEqual("STATUS_NOT_CLAIMABLE", refusal["error_code"])

            code, refusal = self.claim_json(target, "claim", "--slug", "no-such-question", "--agent-id", "agent-a")
            self.assertEqual(2, code)
            self.assertEqual("SLUG_UNKNOWN", refusal["error_code"])

    def test_claim_preserves_other_frontmatter_and_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            page = target / "wiki" / "questions" / "which-benchmarks.md"
            before = self.page_frontmatter(target, "which-benchmarks")
            body_before = page.read_text().split("\n---\n", 1)[1]

            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            after = self.page_frontmatter(target, "which-benchmarks")
            for field in ("type", "created", "priority", "origin", "question", "summary", "source_ids"):
                self.assertEqual(before.get(field), after.get(field), f"field {field} must be preserved")
            self.assertEqual(body_before, page.read_text().split("\n---\n", 1)[1])

    def test_question_status_json_exposes_claim_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = QUESTION_STATUS.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, code)
            payload = json.loads(stdout.getvalue())

            self.assertIn("generated_at", payload)
            claimed = {record["slug"]: record for record in payload["questions"]}
            self.assertEqual("agent-a", claimed["which-benchmarks"]["claimed_by"])
            self.assertIsNotNone(claimed["which-benchmarks"]["claimed_at"])
            self.assertIsNone(claimed["second-question"]["claimed_by"])


class ClaimLintTests(ClaimTestBase):
    """E20-T01: lint coverage for claim hygiene."""

    def run_lint(self, target: Path) -> dict:
        return LINT.run_checks(target, LINT.load_config(target))

    def issues_for(self, results: dict, category: str) -> list[dict]:
        return [issue for issue in results["issues"] if issue["category"] == category]

    def test_claimed_question_passes_lint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")

            results = self.run_lint(target)

            self.assertEqual([], self.issues_for(results, "question_claim_missing"))
            self.assertEqual([], self.issues_for(results, "question_claim_stale"))

    def test_in_progress_without_claim_fields_fires_medium(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            page = target / "wiki" / "questions" / "which-benchmarks.md"
            page.write_text(page.read_text().replace("status: open", "status: in_progress", 1))

            results = self.run_lint(target)

            issues = self.issues_for(results, "question_claim_missing")
            self.assertEqual(1, len(issues))
            self.assertEqual("MEDIUM", issues[0]["severity"])
            self.assertIn("which-benchmarks", issues[0]["message"])
            self.assertIn("question_claim.py", issues[0]["recommendation"])

    def test_stale_claim_fires_low_with_configured_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.claim_json(target, "claim", "--slug", "which-benchmarks", "--agent-id", "agent-a")
            self.set_claimed_at(target, "which-benchmarks", hours_ago=30)

            results = self.run_lint(target)
            issues = self.issues_for(results, "question_claim_stale")
            self.assertEqual(1, len(issues))
            self.assertEqual("LOW", issues[0]["severity"])
            self.assertIn("agent-a", issues[0]["message"])

            # Widen the window via the run block; the same claim is no longer stale.
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text())
            config["run"] = {"claim_staleness_hours": 48}
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))
            results = self.run_lint(target)
            self.assertEqual([], self.issues_for(results, "question_claim_stale"))


class RunBudgetTests(ClaimTestBase):
    """E20-T02: run budgets in config, profile, and status output."""

    def status_json(self, target: Path) -> dict:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = STATUS.main(["--project-root", str(target), "--format", "json"])
        self.assertEqual(0, code)
        return json.loads(stdout.getvalue())

    def test_status_reports_default_budgets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))

            document = self.status_json(target)

            self.assertEqual(
                {
                    "max_questions_per_run": 25,
                    "max_source_requests_per_run": 10,
                    "claim_staleness_hours": 24,
                    "stale_run_threshold_hours": 4,
                    "max_releases_per_run": 75,
                    "max_open_questions_total": 250,
                    "max_intake_per_hour": 25,
                    "max_mcp_intake_batch_questions": 100,
                    "max_discovery_results_per_run": 50,
                    "max_academic_provider_requests_per_run": 25,
                    "max_manual_url_deliveries_per_run": 10,
                    "max_web_downloads_per_run": 10,
                    "max_acquisition_downloads_per_run": 10,
                    "max_github_archive_bytes_per_run": 104857600,
                },
                document["run"],
            )

    def test_profile_run_block_overrides_and_derives_release_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(
                Path(tmpdir),
                run_block={"max_questions_per_run": 5, "max_source_requests_per_run": 2},
            )

            config = yaml.safe_load((target / "research.yml").read_text())
            self.assertEqual(5, config["run"]["max_questions_per_run"])
            self.assertEqual(2, config["run"]["max_source_requests_per_run"])

            document = self.status_json(target)
            self.assertEqual(5, document["run"]["max_questions_per_run"])
            self.assertEqual(2, document["run"]["max_source_requests_per_run"])
            self.assertEqual(15, document["run"]["max_releases_per_run"])
            self.assertEqual(250, document["run"]["max_open_questions_total"])
            self.assertEqual(25, document["run"]["max_intake_per_hour"])
            self.assertEqual(100, document["run"]["max_mcp_intake_batch_questions"])
            self.assertEqual(24, document["run"]["claim_staleness_hours"])

    def test_profile_run_block_can_override_release_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(
                Path(tmpdir),
                run_block={
                    "max_questions_per_run": 5,
                    "max_source_requests_per_run": 2,
                    "max_releases_per_run": 8,
                    "max_open_questions_total": 7,
                    "max_intake_per_hour": 3,
                    "max_mcp_intake_batch_questions": 11,
                },
            )

            config = yaml.safe_load((target / "research.yml").read_text())
            self.assertEqual(8, config["run"]["max_releases_per_run"])
            self.assertEqual(7, config["run"]["max_open_questions_total"])
            self.assertEqual(3, config["run"]["max_intake_per_hour"])
            self.assertEqual(11, config["run"]["max_mcp_intake_batch_questions"])

            document = self.status_json(target)
            self.assertEqual(8, document["run"]["max_releases_per_run"])
            self.assertEqual(7, document["run"]["max_open_questions_total"])
            self.assertEqual(3, document["run"]["max_intake_per_hour"])
            self.assertEqual(11, document["run"]["max_mcp_intake_batch_questions"])

    def test_profile_rejects_invalid_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            profile = yaml.safe_load(PROFILE_FIXTURE_PATH.read_text())
            profile["workspace_init"]["target_path"] = str(root / "bad-workspace")
            profile["workspace_init"]["run"] = {"max_questions_per_run": 0}
            profile_path = root / "profile.yml"
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_questions_per_run", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_releases_per_run": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_releases_per_run", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_open_questions_total": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_open_questions_total", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_intake_per_hour": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_intake_per_hour", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_mcp_intake_batch_questions": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_mcp_intake_batch_questions", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_discovery_results_per_run": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_discovery_results_per_run", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_academic_provider_requests_per_run": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_academic_provider_requests_per_run", str(caught.exception))

            profile["workspace_init"]["run"] = {"max_manual_url_deliveries_per_run": 0}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    INIT.main(["--profile", str(profile_path)])
            self.assertIn("run.max_manual_url_deliveries_per_run", str(caught.exception))

    def test_status_falls_back_on_invalid_config_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            config_path = target / "research.yml"
            config = yaml.safe_load(config_path.read_text())
            config["run"] = {
                "max_questions_per_run": "lots",
                "claim_staleness_hours": -3,
                "max_mcp_intake_batch_questions": "many",
            }
            config_path.write_text(yaml.safe_dump(config, sort_keys=False))

            document = self.status_json(target)

            self.assertEqual(25, document["run"]["max_questions_per_run"])
            self.assertEqual(75, document["run"]["max_releases_per_run"])
            self.assertEqual(250, document["run"]["max_open_questions_total"])
            self.assertEqual(25, document["run"]["max_intake_per_hour"])
            self.assertEqual(100, document["run"]["max_mcp_intake_batch_questions"])
            self.assertEqual(50, document["run"]["max_discovery_results_per_run"])
            self.assertEqual(25, document["run"]["max_academic_provider_requests_per_run"])
            self.assertEqual(10, document["run"]["max_manual_url_deliveries_per_run"])
            self.assertEqual(10, document["run"]["max_acquisition_downloads_per_run"])
            self.assertEqual(104857600, document["run"]["max_github_archive_bytes_per_run"])
            self.assertEqual(24, document["run"]["claim_staleness_hours"])


class VerificationFieldTests(ClaimTestBase):
    """E20-T05: confidence/evidence_strength schema and export propagation."""

    def resolve_answered(self, target: Path, slug: str, extra_fields: dict) -> None:
        answer_dir = target / "wiki" / "synthesis"
        answer_dir.mkdir(parents=True, exist_ok=True)
        (answer_dir / "benchmarks-answer.md").write_text(
            "---\n"
            "type: synthesis\n"
            "created: 2026-06-11\n"
            "updated: 2026-06-11\n"
            "source_ids: []\n"
            "summary: Benchmarks that matter for reasoning evaluation.\n"
            "---\n\n# Benchmarks Answer\n\nThe answer body.\n"
        )
        page = target / "wiki" / "questions" / f"{slug}.md"
        replacement = "status: answered\nanswer_page: ../synthesis/benchmarks-answer.md"
        for field, value in extra_fields.items():
            replacement += f"\n{field}: {value}"
        page.write_text(page.read_text().replace("status: open", replacement, 1))

    def test_verification_fields_pass_lint_and_export(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.resolve_answered(
                target,
                "which-benchmarks",
                {"confidence": "high", "evidence_strength": "corroborated"},
            )

            results = LINT.run_checks(target, LINT.load_config(target))
            frontmatter_issues = [
                issue
                for issue in results["issues"]
                if issue["category"] == "frontmatter" and issue.get("field") in ("confidence", "evidence_strength")
            ]
            self.assertEqual([], frontmatter_issues)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = EXPORT.main(["--project-root", str(target), "--format", "json"])
            self.assertEqual(0, code)
            export = json.loads(stdout.getvalue())
            record = next(item for item in export["questions"] if item["slug"] == "which-benchmarks")
            self.assertEqual("high", record["confidence"])
            self.assertEqual("corroborated", record["evidence_strength"])
            unverified = next(item for item in export["questions"] if item["slug"] == "second-question")
            self.assertNotIn("confidence", unverified)
            self.assertNotIn("evidence_strength", unverified)

    def test_invalid_verification_values_fire_lint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            self.resolve_answered(
                target,
                "which-benchmarks",
                {"confidence": "absolute", "evidence_strength": "vibes"},
            )

            results = LINT.run_checks(target, LINT.load_config(target))
            flagged_fields = {
                issue.get("field")
                for issue in results["issues"]
                if issue["category"] == "frontmatter" and issue["severity"] == "MEDIUM"
            }
            self.assertIn("confidence", flagged_fields)
            self.assertIn("evidence_strength", flagged_fields)


if __name__ == "__main__":
    unittest.main()
