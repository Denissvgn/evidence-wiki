import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
INIT_SCRIPT_PATH = SCRIPTS / "init_research_workspace.py"
RUN_CONTROLLER_SCRIPT_PATH = SCRIPTS / "run_controller.py"
GC_SCRIPT_PATH = SCRIPTS / "workspace_gc.py"
LOCKS_SCRIPT_PATH = SCRIPTS / "_workspace_locks.py"


def load_script_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"missing workspace script: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WorkspaceGcTests(unittest.TestCase):
    def init_workspace(self, root: Path) -> Path:
        init = load_script_module("workspace_gc_init", INIT_SCRIPT_PATH)
        target = root / "workspace"
        with contextlib.redirect_stdout(io.StringIO()):
            code = init.main(
                [
                    "--target",
                    str(target),
                    "--project-name",
                    "workspace-gc",
                    "--project-description",
                    "Workspace GC test fixture.",
                ]
            )
        self.assertEqual(0, int(code or 0))
        return target

    def run_controller(self, target: Path, *args: str) -> dict:
        controller = load_script_module("workspace_gc_run_controller", RUN_CONTROLLER_SCRIPT_PATH)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = controller.main(["--project-root", str(target), *args, "--format", "json"])
        self.assertEqual(0, int(code or 0), stderr.getvalue())
        return json.loads(stdout.getvalue())

    def run_gc(self, target: Path, *args: str) -> tuple[int, dict]:
        gc = load_script_module("workspace_gc_under_test", GC_SCRIPT_PATH)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gc.main(["--project-root", str(target), *args, "--format", "json"])
        self.assertEqual("", stderr.getvalue())
        return int(code or 0), json.loads(stdout.getvalue())

    def rewrite_run_updated_at(self, target: Path, run_id: str, updated_at: str) -> None:
        path = target / "runs" / run_id / "run-state.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["updated_at"] = updated_at
        path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    def test_dry_run_reports_terminal_archives_without_mutating_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            terminal_run = "run-2026-07-04T010203Z-terminal"
            active_run = "run-2026-07-04T010203Z-active"
            self.run_controller(target, "start", "--run-id", terminal_run, "--agent-id", "agent-pm")
            self.run_controller(target, "finish", "--run-id", terminal_run, "--agent-id", "agent-pm", "--final-verdict", "failed")
            self.run_controller(target, "start", "--run-id", active_run, "--agent-id", "agent-pm")
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.rewrite_run_updated_at(target, terminal_run, old)
            self.rewrite_run_updated_at(target, active_run, old)

            code, report = self.run_gc(target, "--older-than-days", "30")

            self.assertEqual(0, code)
            self.assertTrue(report["dry_run"])
            self.assertEqual(1, report["counts"]["eligible"])
            self.assertEqual(0, report["counts"]["archived"])
            self.assertEqual(terminal_run, report["actions"][0]["run_id"])
            self.assertTrue((target / "runs" / terminal_run).is_dir())
            self.assertTrue((target / "runs" / active_run).is_dir())
            self.assertTrue((target / "raw").is_dir())
            self.assertTrue((target / "wiki").is_dir())

    def test_retention_uses_utc_instants_across_offsets_and_dst_boundaries(self):
        gc = load_script_module("workspace_gc_utc_semantics", GC_SCRIPT_PATH)
        self.assertEqual(
            gc.parse_timestamp("2026-11-01T01:45:00-04:00"),
            gc.parse_timestamp("2026-11-01T05:45:00Z"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_ids = ("run-offset-east", "run-offset-west")
            for run_id in run_ids:
                self.run_controller(target, "start", "--run-id", run_id, "--agent-id", "agent-pm")
                self.run_controller(target, "finish", "--run-id", run_id, "--agent-id", "agent-pm", "--final-verdict", "failed")

            old_utc = (datetime.now(timezone.utc) - timedelta(days=45)).replace(microsecond=0)
            east = old_utc.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
            west = old_utc.astimezone(timezone(-timedelta(hours=4))).isoformat()
            self.rewrite_run_updated_at(target, run_ids[0], east)
            self.rewrite_run_updated_at(target, run_ids[1], west)

            eligible = gc.eligible_runs(target, 30)

            self.assertEqual(set(run_ids), {action["run_id"] for action in eligible})

    def test_apply_archives_only_eligible_terminal_run_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            terminal_run = "run-2026-07-04T020304Z-terminal"
            active_run = "run-2026-07-04T020304Z-active"
            self.run_controller(target, "start", "--run-id", terminal_run, "--agent-id", "agent-pm")
            self.run_controller(target, "finish", "--run-id", terminal_run, "--agent-id", "agent-pm", "--final-verdict", "failed")
            self.run_controller(target, "start", "--run-id", active_run, "--agent-id", "agent-pm")
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.rewrite_run_updated_at(target, terminal_run, old)
            self.rewrite_run_updated_at(target, active_run, old)

            code, report = self.run_gc(target, "--older-than-days", "30", "--apply")

            self.assertEqual(0, code)
            self.assertFalse(report["dry_run"])
            self.assertEqual(1, report["counts"]["eligible"])
            self.assertEqual(1, report["counts"]["archived"])
            archive_path = target / report["actions"][0]["archive_path"]
            self.assertTrue(archive_path.is_file())
            self.assertFalse((target / "runs" / terminal_run).exists())
            self.assertTrue((target / "runs" / active_run).is_dir())
            self.assertTrue((target / "raw").is_dir())
            self.assertTrue((target / "wiki").is_dir())
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
            self.assertIn(f"{terminal_run}/run-state.json", names)
            self.assertIn(f"{terminal_run}/events.jsonl", names)
            self.assertFalse(any(".locks" in Path(name).parts for name in names))

    def test_apply_uses_unique_temp_archive_and_preserves_existing_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_id = "run-2026-07-04T030405Z-terminal"
            self.run_controller(target, "start", "--run-id", run_id, "--agent-id", "agent-pm")
            self.run_controller(target, "finish", "--run-id", run_id, "--agent-id", "agent-pm", "--final-verdict", "failed")
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.rewrite_run_updated_at(target, run_id, old)
            gc = load_script_module("workspace_gc_unique_archive", GC_SCRIPT_PATH)
            evaluated_at = datetime.now(timezone.utc)
            action = gc.eligible_runs(target, 30, now=evaluated_at)[0]
            archive_path = target / action["archive_path"]
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            old_fixed_temp = archive_path.with_name(f".{archive_path.name}.tmp")
            old_fixed_temp.write_bytes(b"unrelated-existing-temp")

            applied, reason = gc.archive_run(target, action, 30, evaluated_at)

            self.assertTrue(applied)
            self.assertIsNone(reason)
            self.assertEqual(b"unrelated-existing-temp", old_fixed_temp.read_bytes())
            self.assertEqual([], list(archive_path.parent.glob(f".{archive_path.name}.*.tmp")))

            second_run = "run-2026-07-04T040506Z-terminal"
            self.run_controller(target, "start", "--run-id", second_run, "--agent-id", "agent-pm")
            self.run_controller(
                target,
                "finish",
                "--run-id",
                second_run,
                "--agent-id",
                "agent-pm",
                "--final-verdict",
                "failed",
            )
            self.rewrite_run_updated_at(target, second_run, old)
            second_action = gc.eligible_runs(target, 30, now=evaluated_at)[0]
            second_archive = target / second_action["archive_path"]
            second_archive.write_bytes(b"preexisting-archive")

            applied, reason = gc.archive_run(target, second_action, 30, evaluated_at)

            self.assertFalse(applied)
            self.assertEqual("archive_exists", reason)
            self.assertEqual(b"preexisting-archive", second_archive.read_bytes())
            self.assertTrue((target / second_action["run_path"]).is_dir())

    def test_apply_revalidates_terminal_state_under_run_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_id = "run-2026-07-04T050607Z-terminal"
            self.run_controller(target, "start", "--run-id", run_id, "--agent-id", "agent-pm")
            self.run_controller(target, "finish", "--run-id", run_id, "--agent-id", "agent-pm", "--final-verdict", "failed")
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.rewrite_run_updated_at(target, run_id, old)
            gc = load_script_module("workspace_gc_revalidate", GC_SCRIPT_PATH)
            evaluated_at = datetime.now(timezone.utc)
            action = gc.eligible_runs(target, 30, now=evaluated_at)[0]
            state_path = target / action["run_path"] / "run-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["state"]["current"] = "in_progress"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            applied, reason = gc.archive_run(target, action, 30, evaluated_at)

            self.assertFalse(applied)
            self.assertEqual("no_longer_eligible", reason)
            self.assertTrue((target / action["run_path"]).is_dir())
            self.assertFalse((target / action["archive_path"]).exists())

    def test_competing_process_holding_run_lock_blocks_archive(self):
        child_code = textwrap.dedent(
            """
            import importlib.util
            import pathlib
            import sys
            import time

            spec = importlib.util.spec_from_file_location("gc_child_locks", sys.argv[1])
            locks = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = locks
            spec.loader.exec_module(locks)
            with locks.workspace_lock(pathlib.Path(sys.argv[2]), purpose="competing run writer"):
                print("locked", flush=True)
                time.sleep(5)
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            run_id = "run-2026-07-04T060708Z-terminal"
            self.run_controller(target, "start", "--run-id", run_id, "--agent-id", "agent-pm")
            self.run_controller(target, "finish", "--run-id", run_id, "--agent-id", "agent-pm", "--final-verdict", "failed")
            old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.rewrite_run_updated_at(target, run_id, old)
            gc = load_script_module("workspace_gc_multiprocess", GC_SCRIPT_PATH)
            evaluated_at = datetime.now(timezone.utc)
            action = gc.eligible_runs(target, 30, now=evaluated_at)[0]
            lock_path = gc.run_lock_path(target / action["run_path"])
            child = subprocess.Popen(
                [sys.executable, "-c", child_code, str(LOCKS_SCRIPT_PATH), str(lock_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual("locked", child.stdout.readline().strip())
                with self.assertRaises(gc.LockUnavailableError):
                    gc.archive_run(
                        target,
                        action,
                        30,
                        evaluated_at,
                        lock_timeout_seconds=0.1,
                    )
            finally:
                child.terminate()
                child.communicate(timeout=5)

            self.assertTrue((target / action["run_path"]).is_dir())
            self.assertFalse((target / action["archive_path"]).exists())

    def test_retention_cutoff_uses_injected_utc_clock_across_offset_timestamps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = self.init_workspace(Path(tmpdir))
            eligible_run = "run-2026-10-02T013000Z-dst-boundary"
            recent_run = "run-2026-10-02T023000Z-dst-boundary"
            for run_id in (eligible_run, recent_run):
                self.run_controller(target, "start", "--run-id", run_id, "--agent-id", "agent-pm")
                self.run_controller(
                    target,
                    "finish",
                    "--run-id",
                    run_id,
                    "--agent-id",
                    "agent-pm",
                    "--final-verdict",
                    "failed",
                )
            self.rewrite_run_updated_at(target, eligible_run, "2026-10-01T21:30:00-04:00")
            self.rewrite_run_updated_at(target, recent_run, "2026-10-01T22:30:00-04:00")
            gc = load_script_module("workspace_gc_deterministic_clock", GC_SCRIPT_PATH)

            report = gc.build_report(
                target,
                older_than_days=30,
                apply=False,
                now=datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc),
            )

        self.assertEqual("2026-11-01T01:30:00Z", report["evaluated_at"])
        self.assertEqual([eligible_run], [action["run_id"] for action in report["actions"]])


if __name__ == "__main__":
    unittest.main()
