"""Holder metadata and contended-vs-unavailable refusals for workspace locks.

The interesting guarantees here are cross-process, so the acceptance cases use
``multiprocessing`` rather than threads: the native backends key on the open
file description, and a test that "contended" in one process would prove
nothing about two hosts racing for one workspace.
"""

import errno
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCKS_PATH = REPO_ROOT / "workspace-template" / "scripts" / "_workspace_locks.py"

CHILD_TIMEOUT_SECONDS = 30.0


def load_locks_module(name: str = "workspace_lock_holder_under_test"):
    spec = importlib.util.spec_from_file_location(name, LOCKS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {LOCKS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def spawn_context():
    """Use spawn everywhere so the child never inherits this process's locks."""
    return multiprocessing.get_context("spawn")


# ---------------------------------------------------------------------------
# Child entry points. These run in a separate interpreter under the spawn start
# method, so they must be importable module-level functions with picklable
# arguments, and they must load their own copy of the lock module.
# ---------------------------------------------------------------------------


def _hold_lock_child(lock_path, results, acquired, release):
    """Take the lock, publish a holder describing this process, and wait."""
    locks = load_locks_module("workspace_lock_holder_child")
    holder = {
        "agent_id": "agent-holder",
        "pid": os.getpid(),
        "command": "next",
        "acquired_at": "2026-08-10T00:00:00Z",
    }
    try:
        with locks.workspace_lock(Path(lock_path), timeout_seconds=0.0, holder=holder, purpose="holder child"):
            # Signalled from inside the block, so the parent only starts the
            # contender once the lock is held and the sidecar is published.
            results.put({"role": "holder", "acquired": True, "holder": holder})
            acquired.set()
            release.wait(CHILD_TIMEOUT_SECONDS)
    except BaseException as exc:  # pragma: no cover - reported to the parent as a failure
        if not acquired.is_set():
            results.put({"role": "holder", "acquired": False, "error": repr(exc)})
            acquired.set()


def _contend_child(lock_path, results):
    """Attempt the lock with no wait and report what the refusal carried."""
    locks = load_locks_module("workspace_lock_contender_child")
    started = time.monotonic()
    try:
        with locks.workspace_lock(
            Path(lock_path),
            timeout_seconds=0.0,
            holder={"agent_id": "agent-contender", "pid": os.getpid()},
            purpose="contender child",
        ):
            results.put({"role": "contender", "acquired": True})
    except locks.LockUnavailableError as exc:
        results.put(
            {
                "role": "contender",
                "acquired": False,
                "error_code": exc.error_code,
                "contended": exc.contended,
                "holder": locks.read_lock_holder(Path(lock_path)),
                "elapsed_seconds": time.monotonic() - started,
            }
        )
    except BaseException as exc:  # pragma: no cover - reported to the parent as a failure
        results.put({"role": "contender", "acquired": False, "error": repr(exc)})


def _race_child(lock_path, results, barrier, release):
    """Race a sibling for one lock; a winner holds until the parent says stop."""
    locks = load_locks_module("workspace_lock_race_child")
    barrier.wait(CHILD_TIMEOUT_SECONDS)
    try:
        with locks.workspace_lock(
            Path(lock_path),
            timeout_seconds=0.0,
            holder={"agent_id": "agent-racer", "pid": os.getpid()},
            purpose="race child",
        ):
            results.put({"acquired": True, "pid": os.getpid()})
            release.wait(CHILD_TIMEOUT_SECONDS)
    except locks.LockUnavailableError as exc:
        results.put(
            {
                "acquired": False,
                "pid": os.getpid(),
                "contended": exc.contended,
                "holder": locks.read_lock_holder(Path(lock_path)),
            }
        )
    except BaseException as exc:  # pragma: no cover - reported to the parent as a failure
        results.put({"acquired": False, "pid": os.getpid(), "error": repr(exc)})


def _hold_lock_until_killed_child(lock_path, results, acquired):
    """Hold the lock forever; the parent SIGKILLs this process mid-hold."""
    locks = load_locks_module("workspace_lock_doomed_child")
    holder = {"agent_id": "agent-doomed", "pid": os.getpid()}
    with locks.workspace_lock(Path(lock_path), timeout_seconds=0.0, holder=holder, purpose="doomed child"):
        results.put({"acquired": True, "pid": os.getpid(), "holder": holder})
        acquired.set()
        time.sleep(CHILD_TIMEOUT_SECONDS)  # pragma: no cover - the parent kills this first


class _FakeFcntl:
    """Counts acquisition attempts; every attempt reports contention."""

    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self):
        self.attempts = 0

    def flock(self, _descriptor, flags):
        if flags & self.LOCK_UN:  # pragma: no cover - release is not reached in these tests
            return
        self.attempts += 1
        raise BlockingIOError(errno.EAGAIN, "synthetic contention")


class _FakeMsvcrt:
    """Counts acquisition attempts; every attempt reports contention."""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.attempts = 0

    def locking(self, _descriptor, mode, _byte_count):
        if mode == self.LK_UNLCK:  # pragma: no cover - release is not reached in these tests
            return
        self.attempts += 1
        raise PermissionError(errno.EACCES, "synthetic sharing violation")


class LockHolderSidecarTests(unittest.TestCase):
    def test_holder_is_published_while_held_and_removed_before_release(self):
        locks = load_locks_module()
        holder = {"agent_id": "agent-a", "pid": os.getpid(), "command": "next"}
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with locks.workspace_lock(lock_path, holder=holder, purpose="publish"):
                self.assertEqual(holder, locks.read_lock_holder(lock_path))
                self.assertTrue(locks.lock_holder_path(lock_path).is_file())

            self.assertFalse(locks.lock_holder_path(lock_path).exists())
            self.assertIsNone(locks.read_lock_holder(lock_path))

    def test_sidecar_path_is_the_lock_name_plus_a_holder_suffix(self):
        locks = load_locks_module()

        holder_path = locks.lock_holder_path(Path("/tmp/.locks/session.lock"))

        self.assertEqual(Path("/tmp/.locks/session.lock.holder.json"), holder_path)
        self.assertEqual(".holder.json", locks.LOCK_HOLDER_SUFFIX)

    def test_omitting_holder_leaves_lock_artifacts_untouched(self):
        """The no-holder path must stay exactly what existing callers already get."""
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            lock_path = directory / "session.lock"

            with locks.workspace_lock(lock_path, purpose="no holder"):
                during = sorted(path.name for path in directory.iterdir())
                self.assertIsNone(locks.read_lock_holder(lock_path))
            after = sorted(path.name for path in directory.iterdir())

            self.assertNotIn("session.lock.holder.json", during)
            self.assertNotIn("session.lock.holder.json", after)
            self.assertIsNone(locks.read_lock_holder(lock_path))

    def test_a_holder_adds_the_sidecar_and_nothing_else(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            plain_dir = Path(tmpdir) / "plain"
            holder_dir = Path(tmpdir) / "holder"

            with locks.workspace_lock(plain_dir / "session.lock", purpose="no holder"):
                plain = sorted(path.name for path in plain_dir.iterdir())
            with locks.workspace_lock(holder_dir / "session.lock", holder={"pid": 1}, purpose="holder"):
                held = sorted(path.name for path in holder_dir.iterdir())

            self.assertEqual(sorted([*plain, "session.lock.holder.json"]), held)

    def test_holder_write_does_not_change_the_lock_file_itself(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with locks.workspace_lock(lock_path, purpose="baseline"):
                pass
            baseline = lock_path.read_bytes()
            with locks.workspace_lock(lock_path, holder={"pid": os.getpid()}, purpose="with holder"):
                self.assertEqual(baseline, lock_path.read_bytes())

            self.assertEqual(baseline, lock_path.read_bytes())

    def test_exclusive_fallback_publishes_a_holder_without_growing_its_payload(self):
        """The sidecar is backend-agnostic; the fallback's own payload is load-bearing."""
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ("exclusive",)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                lock_path = Path(tmpdir) / "session.lock"
                holder = {"agent_id": "agent-fallback", "pid": os.getpid()}

                with locks.workspace_lock(lock_path, holder=holder, purpose="fallback") as handle:
                    self.assertEqual("exclusive", handle.backend)
                    self.assertEqual(holder, locks.read_lock_holder(lock_path))
                    payload = locks._exclusive_lock_path(lock_path).read_text(encoding="utf-8")

                keys = [line.partition("=")[0] for line in payload.splitlines() if line]
                self.assertEqual(["pid", "created_at", "ownership_token"], keys)
                self.assertFalse(locks.lock_holder_path(lock_path).exists())
        finally:
            locks.LOCK_BACKENDS = old_backends

    def test_a_new_holder_overwrites_a_crashed_predecessors_sidecar(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            locks.lock_holder_path(lock_path).parent.mkdir(parents=True, exist_ok=True)
            locks.lock_holder_path(lock_path).write_text(
                json.dumps({"agent_id": "crashed-agent", "pid": 999999}),
                encoding="utf-8",
            )

            successor = {"agent_id": "agent-successor", "pid": os.getpid()}
            with locks.workspace_lock(lock_path, holder=successor, purpose="successor"):
                self.assertEqual(successor, locks.read_lock_holder(lock_path))

            self.assertFalse(locks.lock_holder_path(lock_path).exists())

    def test_holder_write_leaves_no_temp_files_behind(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            lock_path = directory / "session.lock"

            with locks.workspace_lock(lock_path, holder={"pid": os.getpid()}, purpose="temp sweep"):
                during = sorted(path.name for path in directory.iterdir())
            after = sorted(path.name for path in directory.iterdir())

            self.assertIn("session.lock.holder.json", during)
            self.assertNotIn("session.lock.holder.json", after)
            self.assertEqual([], [name for name in during + after if name.endswith(".tmp")])

    def test_an_unserializable_holder_fails_loudly_and_still_releases_the_lock(self):
        """A caller bug must not silently degrade to an anonymous lock, nor leak the lock."""
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with self.assertRaises(TypeError):
                with locks.workspace_lock(lock_path, holder={"handle": object()}, purpose="bad holder"):
                    pass  # pragma: no cover - the holder write refuses before the body runs

            self.assertFalse(locks.lock_holder_path(lock_path).exists())
            with locks.workspace_lock(lock_path, timeout_seconds=0.0, purpose="reacquire") as handle:
                self.assertTrue(handle.locked)


class ReadLockHolderTests(unittest.TestCase):
    def read_back(self, payload: bytes) -> object:
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            locks.lock_holder_path(lock_path).write_bytes(payload)
            return locks.read_lock_holder(lock_path)

    def test_missing_sidecar_reads_as_no_holder(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(locks.read_lock_holder(Path(tmpdir) / "session.lock"))

    def test_unreadable_and_non_object_payloads_never_raise(self):
        cases = {
            "truncated json": b'{"agent_id": "a"',
            "empty file": b"",
            "not utf-8": b"\xff\xfe\x00",
            "json array": b'["agent-a"]',
            "json string": b'"agent-a"',
            "json null": b"null",
            "json number": b"12",
            "not json at all": b"pid=1\ncreated_at=0\n",
        }
        for label, payload in cases.items():
            with self.subTest(payload=label):
                self.assertIsNone(self.read_back(payload))

    def test_a_directory_in_the_sidecar_slot_reads_as_no_holder(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            locks.lock_holder_path(lock_path).mkdir()

            self.assertIsNone(locks.read_lock_holder(lock_path))

    def test_a_lock_path_with_no_filename_reads_as_no_holder(self):
        """Never-raises has to hold for a malformed argument too, not just bad bytes."""
        locks = load_locks_module()

        self.assertIsNone(locks.read_lock_holder(Path(os.sep)))

    def test_a_deeply_nested_payload_reads_as_no_holder(self):
        """A hostile or buggy peer must not turn a refusal path into a crash."""
        payload = (b"[" * 100_000) + (b"]" * 100_000)

        self.assertIsNone(self.read_back(payload))

    def test_an_arbitrary_object_round_trips_unvalidated(self):
        """The module treats the holder as opaque: no required keys, no schema."""
        locks = load_locks_module()
        holder = {"totally": "different", "shape": [1, 2, {"nested": True}], "count": 3}
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with locks.workspace_lock(lock_path, holder=holder, purpose="opaque"):
                self.assertEqual(holder, locks.read_lock_holder(lock_path))


class ContentionSignalTests(unittest.TestCase):
    def test_existing_constructions_default_to_not_contended(self):
        locks = load_locks_module()

        plain = locks.LockUnavailableError("no backend")
        detailed = locks.LockUnavailableError("no backend", details={"path": "x"}, remediation="retry later")

        self.assertFalse(plain.contended)
        self.assertFalse(detailed.contended)
        self.assertEqual("LOCK_UNAVAILABLE", plain.error_code)
        self.assertEqual({"path": "x"}, detailed.details)
        self.assertEqual("retry later", detailed.remediation)
        self.assertTrue(locks.LockUnavailableError("busy", contended=True).contended)

    def test_a_held_lock_refuses_a_second_acquisition_as_contention(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with locks.workspace_lock(lock_path, holder={"agent_id": "agent-a"}, purpose="first"):
                with self.assertRaises(locks.LockUnavailableError) as context:
                    with locks.workspace_lock(lock_path, timeout_seconds=0.0, purpose="second"):
                        pass  # pragma: no cover - the second acquisition always refuses

            self.assertTrue(context.exception.contended)
            self.assertEqual("LOCK_UNAVAILABLE", context.exception.error_code)

    def test_absent_backends_are_not_reported_as_contention(self):
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                with self.assertRaises(locks.LockUnavailableError) as context:
                    with locks.workspace_lock(Path(tmpdir) / "session.lock", purpose="no backend"):
                        pass  # pragma: no cover - acquisition always fails with no backends
        finally:
            locks.LOCK_BACKENDS = old_backends

        self.assertFalse(context.exception.contended)
        self.assertIn("unsupported_backends", context.exception.details)

    def test_zero_timeout_makes_exactly_one_fcntl_attempt(self):
        locks = load_locks_module()
        fake = _FakeFcntl()
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            locks, "fcntl", fake
        ), mock.patch.object(locks, "_sleep_until") as sleep:
            with self.assertRaises(locks.LockUnavailableError) as context:
                with locks.workspace_lock(Path(tmpdir) / "session.lock", timeout_seconds=0.0, purpose="fcntl"):
                    pass  # pragma: no cover - the fake backend always refuses

        self.assertEqual(1, fake.attempts)
        sleep.assert_not_called()
        self.assertTrue(context.exception.contended)

    def test_zero_timeout_makes_exactly_one_msvcrt_attempt(self):
        locks = load_locks_module()
        fake = _FakeMsvcrt()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ("msvcrt",)
        try:
            with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
                locks, "msvcrt", fake
            ), mock.patch.object(locks, "_sleep_until") as sleep:
                with self.assertRaises(locks.LockUnavailableError) as context:
                    with locks.workspace_lock(Path(tmpdir) / "session.lock", timeout_seconds=0.0, purpose="msvcrt"):
                        pass  # pragma: no cover - the fake backend always refuses
        finally:
            locks.LOCK_BACKENDS = old_backends

        self.assertEqual(1, fake.attempts)
        sleep.assert_not_called()
        self.assertTrue(context.exception.contended)

    def test_zero_timeout_makes_exactly_one_exclusive_attempt(self):
        """The fallback checks the deadline before it retries or breaks a stale lock."""
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ("exclusive",)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                lock_path = Path(tmpdir) / "session.lock"
                exclusive_path = locks._exclusive_lock_path(lock_path)
                exclusive_path.write_text("pid=999999\ncreated_at=0\nownership_token=held\n", encoding="utf-8")
                incumbent = exclusive_path.read_bytes()

                with mock.patch.object(locks, "_sleep_until") as sleep, mock.patch.object(
                    locks, "_stale_exclusive_lock_observation"
                ) as observe:
                    with self.assertRaises(locks.LockUnavailableError) as context:
                        with locks.workspace_lock(lock_path, timeout_seconds=0.0, purpose="exclusive"):
                            pass  # pragma: no cover - the incumbent lock file always refuses

                sleep.assert_not_called()
                observe.assert_not_called()
                self.assertTrue(context.exception.contended)
                self.assertEqual(incumbent, exclusive_path.read_bytes())
        finally:
            locks.LOCK_BACKENDS = old_backends

    def test_zero_timeout_refuses_promptly(self):
        locks = load_locks_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            with locks.workspace_lock(lock_path, purpose="incumbent"):
                started = time.monotonic()
                with self.assertRaises(locks.LockUnavailableError):
                    with locks.workspace_lock(lock_path, timeout_seconds=0.0, purpose="refused"):
                        pass  # pragma: no cover - the incumbent always wins
                elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)


class SingleWriterEscapeHatchTests(unittest.TestCase):
    def test_single_writer_writes_no_sidecar_and_still_yields(self):
        locks = load_locks_module()
        old_backends = locks.LOCK_BACKENDS
        locks.LOCK_BACKENDS = ()
        try:
            with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
                os.environ, {"EVIDENCE_WIKI_SINGLE_WRITER": "1"}, clear=False
            ):
                directory = Path(tmpdir)
                lock_path = directory / "session.lock"

                with locks.workspace_lock(lock_path, holder={"agent_id": "agent-a"}, purpose="hatch") as handle:
                    self.assertFalse(handle.locked)
                    self.assertTrue(handle.single_writer)
                    self.assertIsNone(locks.read_lock_holder(lock_path))

                self.assertEqual([], sorted(path.name for path in directory.iterdir()))
        finally:
            locks.LOCK_BACKENDS = old_backends

    def test_single_writer_forfeits_driver_detection_under_real_contention(self):
        """Documented degradation: the hatch swallows contention, so no peer is ever named."""
        locks = load_locks_module()
        incumbent = {"agent_id": "agent-incumbent", "pid": os.getpid()}
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"

            with locks.workspace_lock(lock_path, holder=incumbent, purpose="incumbent"):
                with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_SINGLE_WRITER": "1"}, clear=False):
                    with locks.workspace_lock(
                        lock_path,
                        timeout_seconds=0.0,
                        holder={"agent_id": "agent-second"},
                        purpose="second driver",
                    ) as handle:
                        self.assertFalse(handle.locked)
                        self.assertTrue(handle.single_writer)
                        # The incumbent's sidecar is neither overwritten nor removed
                        # by a driver that never actually took the lock.
                        self.assertEqual(incumbent, locks.read_lock_holder(lock_path))

                self.assertEqual(incumbent, locks.read_lock_holder(lock_path))


class MultiprocessContentionTests(unittest.TestCase):
    """Genuine two-process races; threads would not contend on a native backend."""

    def collect(self, results, count: int) -> list[dict]:
        payloads = []
        for _ in range(count):
            payloads.append(results.get(timeout=CHILD_TIMEOUT_SECONDS))
        return payloads

    def stop(self, processes) -> None:
        for process in processes:
            if process.is_alive():  # pragma: no cover - only reached when a child hangs
                process.terminate()
            process.join(timeout=CHILD_TIMEOUT_SECONDS)

    def test_a_losing_process_reports_contention_and_names_the_holder(self):
        context = spawn_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            results = context.Queue()
            acquired = context.Event()
            release = context.Event()
            holder_process = context.Process(target=_hold_lock_child, args=(str(lock_path), results, acquired, release))
            contender = context.Process(target=_contend_child, args=(str(lock_path), results))
            try:
                holder_process.start()
                self.assertTrue(acquired.wait(CHILD_TIMEOUT_SECONDS), "the holder process never acquired the lock")
                holder_payload = results.get(timeout=CHILD_TIMEOUT_SECONDS)
                self.assertTrue(holder_payload.get("acquired"), holder_payload)

                contender.start()
                refusal = results.get(timeout=CHILD_TIMEOUT_SECONDS)
            finally:
                release.set()
                self.stop([holder_process, contender])

            self.assertFalse(refusal.get("acquired"), refusal)
            self.assertEqual("LOCK_UNAVAILABLE", refusal["error_code"])
            self.assertTrue(refusal["contended"])
            self.assertEqual(holder_payload["holder"], refusal["holder"])
            self.assertEqual(holder_process.pid, refusal["holder"]["pid"])
            self.assertNotEqual(holder_process.pid, contender.pid)
            self.assertLess(refusal["elapsed_seconds"], 5.0)

    def test_two_racing_processes_produce_exactly_one_winner(self):
        context = spawn_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            results = context.Queue()
            barrier = context.Barrier(2)
            release = context.Event()
            processes = [
                context.Process(target=_race_child, args=(str(lock_path), results, barrier, release))
                for _ in range(2)
            ]
            try:
                for process in processes:
                    process.start()
                payloads = self.collect(results, 2)
            finally:
                release.set()
                self.stop(processes)

            winners = [payload for payload in payloads if payload.get("acquired")]
            losers = [payload for payload in payloads if not payload.get("acquired")]
            self.assertEqual(1, len(winners), payloads)
            self.assertEqual(1, len(losers), payloads)
            self.assertNotIn("error", losers[0], losers[0])
            self.assertTrue(losers[0]["contended"])
            # The winner publishes its sidecar just after acquiring, so a loser
            # refused inside that window legitimately sees nothing; when it does
            # see a holder, that holder is the winner.
            if losers[0]["holder"] is not None:
                self.assertEqual(winners[0]["pid"], losers[0]["holder"]["pid"])

    def test_a_killed_holder_leaves_the_lock_acquirable_and_its_sidecar_overwritten(self):
        locks = load_locks_module()
        if not locks.multiprocess_lock_supported():  # pragma: no cover - platform dependent
            self.skipTest("no native advisory backend; crash release is best-effort on the fallback")
        context = spawn_context()
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "session.lock"
            results = context.Queue()
            acquired = context.Event()
            doomed = context.Process(target=_hold_lock_until_killed_child, args=(str(lock_path), results, acquired))
            try:
                doomed.start()
                self.assertTrue(acquired.wait(CHILD_TIMEOUT_SECONDS), "the doomed process never acquired the lock")
                doomed_payload = results.get(timeout=CHILD_TIMEOUT_SECONDS)
                doomed.kill()
                doomed.join(timeout=CHILD_TIMEOUT_SECONDS)
            finally:
                self.stop([doomed])

            self.assertEqual(doomed_payload["holder"], locks.read_lock_holder(lock_path))

            successor = {"agent_id": "agent-successor", "pid": os.getpid()}
            with locks.workspace_lock(lock_path, timeout_seconds=0.0, holder=successor, purpose="successor"):
                self.assertEqual(successor, locks.read_lock_holder(lock_path))

            self.assertFalse(locks.lock_holder_path(lock_path).exists())


if __name__ == "__main__":
    unittest.main()
