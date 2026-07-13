import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKS_PATH = REPO_ROOT / "workspace-template" / "scripts" / "_workspace_locks.py"


def load_locks_module():
    spec = importlib.util.spec_from_file_location("workspace_locks_under_test", LOCKS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {LOCKS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WorkspaceLockTests(unittest.TestCase):
    def test_capability_reports_current_interpreter_backends(self):
        locks = load_locks_module()

        capability = locks.lock_capability()

        self.assertEqual(list(locks.available_lock_backends()), capability["available_backends"])
        self.assertEqual(bool(capability["native_backends"]), capability["multiprocess_safe"])
        self.assertEqual(bool(capability["available_backends"]), capability["multiprocess_coordination_available"])
        self.assertEqual(capability["multiprocess_safe"], locks.multiprocess_lock_supported())
        self.assertIn("exclusive", capability["available_backends"])
        self.assertEqual("exclusive", capability["fallback_backend"])
        self.assertIn("best-effort", capability["fallback_guarantee"])

    def test_capability_honors_configured_backend_set(self):
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ()
        try:
            self.assertEqual((), locks.available_lock_backends())
            self.assertFalse(locks.multiprocess_lock_supported())
            self.assertFalse(locks.lock_capability()["multiprocess_safe"])
        finally:
            locks.LOCK_BACKENDS = old_backends

    def test_exclusive_only_capability_does_not_claim_native_safety(self):
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ("exclusive",)
        try:
            capability = locks.lock_capability()
            self.assertFalse(capability["multiprocess_safe"])
            self.assertTrue(capability["multiprocess_coordination_available"])
            self.assertFalse(locks.multiprocess_lock_supported())
            self.assertIn("best-effort", capability["fallback_guarantee"])
        finally:
            locks.LOCK_BACKENDS = old_backends

    def test_lock_acquires_and_releases_on_current_platform(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"

            with locks.workspace_lock(lock_path, purpose="unit test"):
                self.assertTrue(lock_path.exists())

            with locks.workspace_lock(lock_path, purpose="unit test reacquire"):
                self.assertTrue(lock_path.exists())

    def test_competing_subprocess_cannot_acquire_held_lock(self):
        locks = load_locks_module()
        child_code = textwrap.dedent(
            """
            import importlib.util
            import json
            import pathlib
            import sys

            spec = importlib.util.spec_from_file_location("child_workspace_locks", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            try:
                with module.workspace_lock(pathlib.Path(sys.argv[2]), timeout_seconds=0.2, purpose="child"):
                    print(json.dumps({"acquired": True}))
            except module.LockUnavailableError as exc:
                print(json.dumps({"acquired": False, "error_code": exc.error_code}))
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"

            with locks.workspace_lock(lock_path, purpose="parent"):
                result = subprocess.run(
                    [sys.executable, "-c", child_code, str(LOCKS_PATH), str(lock_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual({"acquired": False, "error_code": "LOCK_UNAVAILABLE"}, payload)

    def test_exclusive_fallback_coordinates_competing_processes(self):
        locks = load_locks_module()
        child_code = textwrap.dedent(
            """
            import importlib.util
            import json
            import pathlib
            import sys

            spec = importlib.util.spec_from_file_location("child_exclusive_locks", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.LOCK_BACKENDS = ("exclusive",)
            try:
                with module.workspace_lock(pathlib.Path(sys.argv[2]), timeout_seconds=0.2, purpose="child"):
                    print(json.dumps({"acquired": True}))
            except module.LockUnavailableError as exc:
                print(json.dumps({"acquired": False, "error_code": exc.error_code}))
            """
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            locks.LOCK_BACKENDS = ("exclusive",)
            try:
                with locks.workspace_lock(lock_path, purpose="parent"):
                    result = subprocess.run(
                        [sys.executable, "-c", child_code, str(LOCKS_PATH), str(lock_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
            finally:
                locks.LOCK_BACKENDS = old_backends

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                {"acquired": False, "error_code": "LOCK_UNAVAILABLE"},
                json.loads(result.stdout),
            )

    def test_unavailable_lock_mechanisms_raise_lock_unavailable(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            locks.LOCK_BACKENDS = ()
            try:
                with self.assertRaises(locks.LockUnavailableError) as context:
                    with locks.workspace_lock(lock_path, purpose="forced failure"):
                        pass
            finally:
                locks.LOCK_BACKENDS = old_backends

            self.assertEqual("LOCK_UNAVAILABLE", context.exception.error_code)
            self.assertIn("retry", context.exception.remediation)
            self.assertIn("do not delete raw evidence", context.exception.remediation)

    def test_exclusive_backend_breaks_stale_lock_file(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            locks.LOCK_BACKENDS = ("exclusive",)
            try:
                exclusive_path = locks._exclusive_lock_path(lock_path)
                exclusive_path.write_text(
                    "pid=999999\ncreated_at=0\nownership_token=stale-owner-token\n",
                    encoding="utf-8",
                )
                old_time = time.time() - (locks.DEFAULT_STALE_EXCLUSIVE_LOCK_SECONDS + 60)
                os.utime(exclusive_path, (old_time, old_time))

                with locks.workspace_lock(lock_path, purpose="stale recovery", timeout_seconds=1.0) as acquired:
                    self.assertEqual("exclusive", acquired.backend)
                    self.assertTrue(exclusive_path.is_file())
                    contents = exclusive_path.read_text(encoding="utf-8")
                    self.assertIn(f"pid={os.getpid()}", contents)
                    self.assertIn("ownership_token=", contents)
            finally:
                locks.LOCK_BACKENDS = old_backends

    def test_stale_break_does_not_unlink_replacement_owner(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            exclusive_path = Path(tmpdir) / "workspace.lock.exclusive"
            exclusive_path.write_text("ownership_token=stale-token\n", encoding="utf-8")
            observed = locks._exclusive_ownership_token(exclusive_path)
            exclusive_path.write_text("ownership_token=successor-token\n", encoding="utf-8")

            removed = locks._break_stale_exclusive_lock(exclusive_path, observed)

            self.assertFalse(removed)
            self.assertTrue(exclusive_path.is_file())
            self.assertIn("successor-token", exclusive_path.read_text(encoding="utf-8"))

    def test_exclusive_release_does_not_unlink_replacement_owner(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            exclusive_path = Path(tmpdir) / "workspace.lock.exclusive"
            acquired = locks._AcquiredBackend(
                "exclusive",
                path=exclusive_path,
                ownership_token="-".join(("original", "token")),
            )
            exclusive_path.write_text("ownership_token=successor-token\n", encoding="utf-8")

            locks._release_exclusive(acquired)

            self.assertTrue(exclusive_path.is_file())
            self.assertIn("successor-token", exclusive_path.read_text(encoding="utf-8"))

    def test_exclusive_backend_does_not_break_fresh_lock_file(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            locks.LOCK_BACKENDS = ("exclusive",)
            try:
                exclusive_path = locks._exclusive_lock_path(lock_path)
                exclusive_path.write_text("pid=999999\ncreated_at=0\n", encoding="utf-8")

                with self.assertRaises(locks.LockUnavailableError):
                    with locks.workspace_lock(lock_path, purpose="fresh lock", timeout_seconds=0.2):
                        pass

                self.assertTrue(exclusive_path.is_file())
                self.assertIn("pid=999999", exclusive_path.read_text(encoding="utf-8"))
            finally:
                locks.LOCK_BACKENDS = old_backends

    def test_exclusive_backend_heartbeat_prevents_live_lock_from_aging_stale(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            locks.LOCK_BACKENDS = ("exclusive",)
            try:
                with locks.workspace_lock(
                    lock_path,
                    purpose="heartbeat owner",
                    stale_exclusive_after_seconds=0.15,
                ):
                    exclusive_path = locks._exclusive_lock_path(lock_path)
                    old_time = time.time() - 60
                    os.utime(exclusive_path, (old_time, old_time))
                    time.sleep(0.08)
                    self.assertLess(time.time() - exclusive_path.stat().st_mtime, 0.15)
                    with self.assertRaises(locks.LockUnavailableError):
                        with locks.workspace_lock(
                            lock_path,
                            purpose="competing owner",
                            timeout_seconds=0.05,
                            stale_exclusive_after_seconds=0.15,
                        ):
                            pass
            finally:
                locks.LOCK_BACKENDS = old_backends

    def test_single_writer_escape_hatch_bypasses_unavailable_mechanisms(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "workspace.lock"
            old_backends = locks.LOCK_BACKENDS
            old_env = locks.os.environ.get("EVIDENCE_WIKI_SINGLE_WRITER")
            locks.LOCK_BACKENDS = ()
            locks.os.environ["EVIDENCE_WIKI_SINGLE_WRITER"] = "1"
            try:
                with locks.workspace_lock(lock_path, purpose="single writer") as acquired:
                    self.assertFalse(acquired.locked)
                    self.assertTrue(acquired.single_writer)
            finally:
                locks.LOCK_BACKENDS = old_backends
                if old_env is None:
                    locks.os.environ.pop("EVIDENCE_WIKI_SINGLE_WRITER", None)
                else:
                    locks.os.environ["EVIDENCE_WIKI_SINGLE_WRITER"] = old_env


if __name__ == "__main__":
    unittest.main()
