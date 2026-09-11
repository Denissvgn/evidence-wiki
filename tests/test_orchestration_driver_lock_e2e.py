"""Exercise driver locking with real processes and initialized workspaces.

A contending driver must report ORCHESTRATION_DRIVER_BUSY and the holder identity
without issuing work. Native locks release after process death, and status remains
readable while a driver holds the lock. Racing starts must create only one session;
the loser may observe an existing session or a busy driver.

Drivers use deployed scripts copied by the real initializer. Exit codes are literal
assertions because shell hosts depend on their numeric values. Waits observe ready
files, process exit and lock release; the status check has an explicit elapsed-time
budget. Timer-based fallback stale recovery is covered in test_workspace_lock_holder;
the process-death case here requires a native lock backend.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests._script_loader import load_module

REPO_ROOT = Path(__file__).resolve().parents[1]

ORCHESTRATION_ID = "orch-driver-lock-e2e"
QUESTION_SLUG = "driver-lock-e2e-question"

#: Process exit codes are asserted as literals because shell hosts depend on them.
EXIT_OK = 0
EXIT_INVALID = 2
#: 6, not 5: ``evidence-wiki orchestrate run``/``resume`` already returns 5 for a
#: failed managed runner, and a caller reading only ``$?`` must not confuse the
#: one status it should retry with one it never should.
EXIT_DRIVER_BUSY = 6

#: How long ``status`` may take while another driver holds the session lock.
#:
#: The failure this bounds is "status quietly started taking the driver lock".
#: The lock module's own default wait is 10 s, so an implementation that took the
#: lock and queued would land just above this budget even in the *best* case for
#: it -- and the holder in these tests never releases until the test says so, so
#: it would in fact exhaust the wait and fail on the exit code too. Below that
#: ceiling the number is chosen for slack, not precision: an unloaded machine
#: answers `status` on this fixture in well under a tenth of a second, so 8 s is
#: roughly two orders of magnitude of headroom for a loaded CI box, a cold page
#: cache, and a fresh interpreter start.
STATUS_BUDGET_SECONDS = 8.0

#: Ceiling for any single controller subprocess. Not a behavioural assertion --
#: it exists so that a driver that genuinely deadlocks fails the suite in a
#: minute instead of hanging the run until CI kills it.
PROCESS_TIMEOUT_SECONDS = 120.0

#: A real second driver, in its own process, parked inside the controller's own
#: critical section.
#:
#: The lock is taken through ``driver_session_lock`` -- the function the real
#: ``next`` uses -- so the holder sidecar that a refused peer reads back is
#: written by the production code path with a real pid, not fabricated by the
#: test. Parking rather than racing is what makes the overlap deterministic:
#: two ``next`` processes launched together may or may not overlap, but a driver
#: that holds the lock until told to let go overlaps every time.
HOLDING_DRIVER = """
import importlib.util, os, sys, time
from pathlib import Path

scripts, project_root, orchestration_id, ready, release, agent_id, command = sys.argv[1:8]
spec = importlib.util.spec_from_file_location(
    "driver_e2e_held_controller", str(Path(scripts) / "orchestration_controller.py")
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

with module.driver_session_lock(
    Path(project_root),
    orchestration_id,
    command=command,
    agent_id=(agent_id or None),
    wait_seconds=0.0,
):
    # Announced by rename so the waiting test can never read a half-written pid.
    marker = Path(ready)
    tmp = marker.with_name(marker.name + ".partial")
    tmp.write_text(str(os.getpid()), encoding="utf-8")
    tmp.replace(marker)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline and not Path(release).exists():
        time.sleep(0.02)
"""


def load_deployed_module(name: str, path: Path):
    """Load a module out of the *initialized workspace*, not the template."""
    return load_module(name, path)


class DriverLockWorkspace:
    """A real installed workspace and the drivers that contend for its session.

    Deliberately not a ``TestCase``: subclassing one that carried tests would
    re-run the whole parent suite under every child class. Same reason as
    `tests/test_delegated_acquisition_e2e.py`'s fixture class.
    """

    _module_serial = 0

    # -- construction ----------------------------------------------------------

    def child_env(self) -> dict[str, str]:
        """The environment every driver in this suite runs under.

        ``EVIDENCE_WIKI_SINGLE_WRITER=1`` bypasses lock acquisition entirely and
        writes no holder sidecar, so under it ``ORCHESTRATION_DRIVER_BUSY`` can
        never fire. A developer who exports it would otherwise watch this suite
        pass while proving nothing at all, which is the one outcome a sign-off
        suite must not have. Removing it here is not a claim about the escape
        hatch -- its documented degradation is pinned by the lock module's own
        tests; it is this suite refusing to be silently disarmed.
        """
        env = dict(os.environ)
        env.pop("EVIDENCE_WIKI_SINGLE_WRITER", None)
        return env

    def init_workspace(self, root: Path, *, question: bool = False) -> Path:
        """Build the workspace the way an operator does: `evidence-wiki init`."""
        workspace = root / "driver lock workspace"
        created = subprocess.run(  # noqa: S603
            [
                sys.executable, "-B", "-m", "evidence_wiki.cli", "init",
                "--target", str(workspace),
                "--project-name", "driver-lock-e2e",
                "--project-description", "Workspace for concurrent driver checks.",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            env=self.child_env(),
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        self.assertEqual(0, created.returncode, created.stderr)
        # The installed CLI is only trustworthy as a fixture if it really laid
        # down the controller these tests then drive.
        self.assertTrue((workspace / "scripts" / "orchestration_controller.py").is_file())

        if question:
            batch = root / "questions.json"
            batch.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {
                                "id": QUESTION_SLUG,
                                "question": "Which evidence answers this question?",
                                "priority": "high",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            intake = self.workspace_script(
                workspace, "intake_questions.py", "--from-file", str(batch), "--format", "json"
            )
            self.assertEqual(0, intake.returncode, intake.stderr)
        return workspace

    def started_workspace(self, root: Path, *, question: bool = True) -> Path:
        workspace = self.init_workspace(root, question=question)
        started = self.controller(workspace, "start", "--orchestration-id", ORCHESTRATION_ID, "--agent-id", "pm-agent")
        self.assertEqual(EXIT_OK, started.returncode, started.stderr)
        return workspace

    def deployed_locks(self, workspace: Path):
        """The lock module as deployed into this workspace.

        Loaded under a unique name because several workspaces (and therefore
        several copies of this module) can exist within one test process.
        """
        DriverLockWorkspace._module_serial += 1
        return load_deployed_module(
            f"driver_e2e_deployed_locks_{DriverLockWorkspace._module_serial}",
            workspace / "scripts" / "_workspace_locks.py",
        )

    # -- drivers ---------------------------------------------------------------

    def workspace_script(self, workspace: Path, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            [
                sys.executable, "-B", str(workspace / "scripts" / script),
                "--project-root", str(workspace),
                *args,
            ],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            env=self.child_env(),
            timeout=PROCESS_TIMEOUT_SECONDS,
        )

    def controller(self, workspace: Path, command: str, *args: str, timeout: float = PROCESS_TIMEOUT_SECONDS):
        return subprocess.run(  # noqa: S603
            [
                sys.executable, "-B", str(workspace / "scripts" / "orchestration_controller.py"),
                "--project-root", str(workspace),
                command, *args,
                "--format", "json",
            ],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=False,
            env=self.child_env(),
            timeout=timeout,
        )

    def hold_session(
        self,
        workspace: Path,
        scratch: Path,
        *,
        agent_id: str = "driver-alpha",
        command: str = "next",
        label: str = "holder",
    ) -> tuple[subprocess.Popen, int, Path]:
        """Start a second driver process and return once it *truly* holds the lock.

        Returns the process, its pid as the holder itself reported it, and the
        release marker. Waiting on the ready file rather than on a sleep is what
        keeps every assertion downstream deterministic.
        """
        ready = scratch / f"{label}-ready"
        release = scratch / f"{label}-release"
        holder = subprocess.Popen(  # noqa: S603
            [
                sys.executable, "-B", "-c", HOLDING_DRIVER,
                str(workspace / "scripts"),
                str(workspace),
                ORCHESTRATION_ID,
                str(ready),
                str(release),
                agent_id,
                command,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.child_env(),
        )

        def stop() -> None:
            """End the holder, tolerating a scratch directory that is already gone.

            Registered as a cleanup so that no failing assertion can leak a
            process sitting on a lock. Cleanups run *after* the test method's
            ``TemporaryDirectory`` has been removed, so the release marker often
            cannot be created at all by this point -- and a holder that can never
            see its marker would otherwise sit out its own deadline while this
            waited on it. Signalling it directly in that case is what keeps a
            cleanup from costing more than the test.
            """
            try:
                release.touch()
            except OSError:
                holder.kill()
            try:
                holder.wait(PROCESS_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                holder.kill()
                holder.wait(PROCESS_TIMEOUT_SECONDS)

        self.addCleanup(stop)

        deadline = time.monotonic() + PROCESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready.is_file():
                return holder, int(ready.read_text(encoding="utf-8")), release
            if holder.poll() is not None:
                self.fail(f"the holding driver exited before acquiring: {holder.communicate()[1]}")
            time.sleep(0.02)
        self.fail("the holding driver never acquired the session lock")

    def release_and_reap(self, holder: subprocess.Popen, release: Path) -> None:
        release.touch()
        self.assertEqual(0, holder.wait(PROCESS_TIMEOUT_SECONDS), holder.communicate()[1])

    # -- reading durable state -------------------------------------------------

    def session_dir(self, workspace: Path) -> Path:
        return workspace / "runs" / "orchestrations" / ORCHESTRATION_ID

    def durable_state(self, workspace: Path) -> tuple[bytes, bytes]:
        """The two files a refusal is forbidden to touch."""
        session = self.session_dir(workspace)
        return (
            (session / "session.json").read_bytes(),
            (session / "events.jsonl").read_bytes(),
        )

    def work_order_ids(self, workspace: Path) -> list[str]:
        orders = self.session_dir(workspace) / "work-orders"
        return sorted(path.stem for path in orders.glob("*.json")) if orders.is_dir() else []

    def events(self, workspace: Path) -> list[dict]:
        events = self.session_dir(workspace) / "events.jsonl"
        return [
            json.loads(line)
            for line in events.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def event_types(self, workspace: Path) -> list[str]:
        return [event.get("event_type") for event in self.events(workspace)]

    def issued_action_ids(self, workspace: Path) -> list[str]:
        return [
            event.get("action_id")
            for event in self.events(workspace)
            if event.get("event_type") == "action_issued"
        ]

    # -- refusal assertions ----------------------------------------------------

    def assert_driver_busy(self, refused: subprocess.CompletedProcess) -> dict:
        """Assert the *specific* refusal, never merely a non-zero exit.

        A driver-busy assertion that accepted "it failed somehow" would pass for
        a workspace that could not be read at all, which is precisely the way an
        end-to-end suite goes quietly vacuous.
        """
        self.assertEqual(EXIT_DRIVER_BUSY, refused.returncode, refused.stderr)
        # Stdout purity: the envelope belongs on stderr, so a host parsing
        # reports off stdout never finds a refusal spliced into one.
        self.assertEqual("", refused.stdout.strip())
        envelope = json.loads(refused.stderr)
        self.assertEqual("ORCHESTRATION_DRIVER_BUSY", envelope["error_code"])
        self.assertTrue(envelope["recoverable"])
        self.assertEqual(ORCHESTRATION_ID, envelope["details"]["orchestration_id"])
        return envelope

    def assert_recent_utc_timestamp(self, value: object) -> None:
        """The holder's ``acquired_at`` is a real UTC instant from this run."""
        self.assertIsInstance(value, str)
        try:
            moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            self.fail(f"holder timestamp is not an ISO-8601 UTC instant: {value!r}")
        age = abs((datetime.now(timezone.utc) - moment).total_seconds())
        # Generous, because the assertion is "this run wrote it", not "the clocks
        # agree to the second": a hardcoded or leftover timestamp fails by hours.
        self.assertLess(age, 600, f"holder timestamp is not from this run: {value}")


class ContendedDriverTests(DriverLockWorkspace, unittest.TestCase):
    """A second driver refuses with a parseable process status and holder identity."""

    def test_a_second_next_process_is_refused_and_the_session_issues_one_work_order(self):
        """One order, one refusal, and nothing written by the loser.

        The overlap is made certain rather than hoped for: the first driver parks
        inside the controller's own critical section until this test lets it go.
        Before driver locking the second process waited ten seconds and then *proceeded*,
        interleaving its writes with the first driver's; the corruption surfaced
        later, somewhere else, with nothing tying it back to the overlap.

        "Exactly one work order issued" is asserted over the whole episode --
        the refused driver issued none, and the successor issued exactly one --
        because a lock that refused loudly and *also* leaked a second order would
        satisfy a narrower reading of the criterion while failing its intent.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.started_workspace(root)
            self.assertEqual([], self.work_order_ids(workspace), "the session starts with no orders")

            holder, holder_pid, release = self.hold_session(workspace, root, agent_id="driver-alpha")
            before = self.durable_state(workspace)

            refused = self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID)

            envelope = self.assert_driver_busy(refused)
            reported = envelope["details"]["holder"]
            self.assertEqual(holder_pid, reported["pid"], "the refusal names some other process")
            self.assertEqual("driver-alpha", reported["agent_id"])
            self.assertEqual("next", reported["command"])
            self.assert_recent_utc_timestamp(reported["acquired_at"])
            # The identity has to reach a human too, not only a JSON parser.
            self.assertIn(str(holder_pid), envelope["message"])
            self.assertIn("driver-alpha", envelope["message"])

            # A refusal is fail-closed: it happens before anything durable moves.
            self.assertEqual(before, self.durable_state(workspace))
            self.assertEqual([], self.work_order_ids(workspace), "the refused driver issued an order")

            # The winner was never obstructed, and a successor proceeds once it lets go.
            self.release_and_reap(holder, release)
            proceeded = self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID)

            self.assertEqual(EXIT_OK, proceeded.returncode, proceeded.stderr)
            order = json.loads(proceeded.stdout)
            self.assertIn("action_id", order)
            self.assertEqual([order["action_id"]], self.work_order_ids(workspace))
            self.assertEqual([order["action_id"]], self.issued_action_ids(workspace))

    def test_a_holder_that_recorded_no_agent_id_is_still_named_by_pid(self):
        """The criterion says "agent id *when supplied*" -- so prove the other half.

        ``next`` and ``submit`` take ``--agent-id`` optionally, and the lock is
        taken before the session is loaded, so a real holder frequently has no
        agent to report. The refusal must still be a refusal: same code, same
        exit status, the holder's pid intact, and the missing name rendered
        rather than crashing the process that is explaining the failure.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.started_workspace(root)
            holder, holder_pid, release = self.hold_session(workspace, root, agent_id="", command="submit")

            refused = self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID)

            envelope = self.assert_driver_busy(refused)
            reported = envelope["details"]["holder"]
            self.assertIsNone(reported["agent_id"])
            self.assertEqual(holder_pid, reported["pid"])
            self.assertEqual("submit", reported["command"], "the refusal says which call is busy")
            self.assertIn("<unrecorded agent>", envelope["message"])
            self.assertIn(str(holder_pid), envelope["message"])
            self.assertEqual([], self.work_order_ids(workspace))
            self.release_and_reap(holder, release)


class CrashedDriverTests(DriverLockWorkspace, unittest.TestCase):
    """A native lock releases when its holder process dies."""

    def test_a_sigkilled_driver_leaves_no_lock_the_next_process_cannot_take(self):
        """SIGKILL is the crash the kernel can tell a successor about.

        Scoped to the native advisory backends deliberately, and skipped rather
        than faked elsewhere. Only fcntl and msvcrt learn of a holder's death
        from the OS; the exclusive-create fallback has no such notification and
        recovers on a heartbeat timer (``DRIVER_LOCK_STALE_FALLBACK_SECONDS``,
        120 s here), which `tests/test_workspace_lock_holder.py` exercises
        against the lock module directly instead of via a two-minute sleep in a
        sign-off suite.

        The crashed driver's holder sidecar is still on disk when the successor
        runs -- asserted, because that leftover is exactly what a successor could
        mistake for a live owner, and a "no stale lock" claim that quietly relied
        on the sidecar being gone would not be testing recovery at all.
        """
        if not hasattr(signal, "SIGKILL"):  # pragma: no cover - platform dependent
            self.skipTest("SIGKILL is not available on this platform")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.started_workspace(root)
            locks = self.deployed_locks(workspace)
            if not locks.multiprocess_lock_supported():  # pragma: no cover - platform dependent
                self.skipTest("no native advisory lock backend on this platform")

            holder, holder_pid, _ = self.hold_session(workspace, root, agent_id="doomed-driver")

            # Contention is real *before* the kill, so what follows measures
            # recovery rather than a lock that was never held.
            self.assert_driver_busy(self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID))

            os.kill(holder_pid, signal.SIGKILL)
            holder.wait(PROCESS_TIMEOUT_SECONDS)
            sidecar = locks.lock_holder_path(
                self.session_dir(workspace) / ".locks" / "session.lock"
            )
            self.assertTrue(sidecar.is_file(), "the crashed driver's holder block should still be on disk")
            self.assertEqual(holder_pid, json.loads(sidecar.read_text(encoding="utf-8"))["pid"])

            proceeded = self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID)

            self.assertEqual(EXIT_OK, proceeded.returncode, proceeded.stderr)
            order = json.loads(proceeded.stdout)
            self.assertIn("action_id", order)
            self.assertEqual([order["action_id"]], self.work_order_ids(workspace))


class StatusIsLockFreeTests(DriverLockWorkspace, unittest.TestCase):
    """Status remains readable while another process holds the driver lock."""

    def test_status_answers_promptly_while_another_driver_holds_the_session(self):
        """Held lock, real poll, real clock.

        The contention is proved in the same breath as the poll: the same held
        lock that lets ``status`` through refuses ``next``. Without that pairing
        a holder which had quietly died would make ``status`` trivially fast and
        this test would pass while proving nothing.

        Two independent things would catch a regression here. Taking the lock at
        the default wait would refuse outright and fail the exit-code assertion;
        taking it with any wait at all would blow the elapsed-time budget, since
        the holder does not release until after both assertions. The budget's
        margin is argued at ``STATUS_BUDGET_SECONDS``.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = self.started_workspace(root)
            holder, _, release = self.hold_session(workspace, root, agent_id="polling-blocker")

            self.assert_driver_busy(self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID))

            started = time.monotonic()
            polled = self.controller(
                workspace,
                "status",
                "--orchestration-id",
                ORCHESTRATION_ID,
                # A `status` that truly waited on an unbounded lock would hang
                # here rather than merely be slow; the timeout turns that into a
                # loud failure instead of a stalled suite.
                timeout=PROCESS_TIMEOUT_SECONDS,
            )
            elapsed = time.monotonic() - started

            self.assertEqual(EXIT_OK, polled.returncode, polled.stderr)
            document = json.loads(polled.stdout)
            self.assertEqual(ORCHESTRATION_ID, document["orchestration_id"])
            self.assertEqual("active", document["status"], "the poll read the live session, not a stub")
            self.assertLess(
                elapsed,
                STATUS_BUDGET_SECONDS,
                "status waited on the driver lock instead of reading past it",
            )

            # The holder really did own the lock for the whole poll: it is still
            # alive here, and it is still refusing peers.
            self.assertIsNone(holder.poll(), "the holder exited mid-test")
            self.assert_driver_busy(self.controller(workspace, "next", "--orchestration-id", ORCHESTRATION_ID))
            self.release_and_reap(holder, release)


class RacingStartTests(DriverLockWorkspace, unittest.TestCase):
    """Concurrent starts create one session and return a structured refusal to the loser.

    A loser arriving after commit sees ORCHESTRATION_EXISTS; one overlapping the lock
    sees ORCHESTRATION_DRIVER_BUSY. Both are valid outcomes. ContendedDriverTests
    separately establishes that lock contention actually reaches the busy refusal.
    """

    def test_racing_starts_leave_exactly_one_session_and_one_intelligible_loser(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = self.init_workspace(Path(tmpdir))
            racers = [
                subprocess.Popen(  # noqa: S603
                    [
                        sys.executable, "-B",
                        str(workspace / "scripts" / "orchestration_controller.py"),
                        "--project-root", str(workspace),
                        "start",
                        "--orchestration-id", ORCHESTRATION_ID,
                        "--agent-id", f"racer-{index}",
                        "--format", "json",
                    ],
                    cwd=str(workspace),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self.child_env(),
                )
                for index in range(3)
            ]
            # Drained with ``communicate`` before any return code is read: a racer
            # that filled a pipe while the test waited on it would deadlock rather
            # than fail.
            outcomes = [(process, *process.communicate(timeout=PROCESS_TIMEOUT_SECONDS)) for process in racers]

            winners = [outcome for outcome in outcomes if outcome[0].returncode == EXIT_OK]
            losers = [outcome for outcome in outcomes if outcome[0].returncode != EXIT_OK]
            self.assertEqual(1, len(winners), [(o[0].returncode, o[2]) for o in outcomes])
            self.assertEqual(ORCHESTRATION_ID, json.loads(winners[0][1])["orchestration_id"])

            for process, _, stderr in losers:
                with self.subTest(returncode=process.returncode):
                    self.assertTrue(stderr.strip().startswith("{"), f"the loser wrote no envelope: {stderr}")
                    envelope = json.loads(stderr)
                    # The pair is asserted as pairs, not as two independent
                    # memberships: a loser that reported "busy" while exiting 2
                    # would be undispatchable by a shell host reading only $?.
                    self.assertIn(
                        (envelope["error_code"], process.returncode),
                        {
                            ("ORCHESTRATION_EXISTS", EXIT_INVALID),
                            ("ORCHESTRATION_DRIVER_BUSY", EXIT_DRIVER_BUSY),
                        },
                        stderr,
                    )

            # The invariant, and the only thing here that would survive a rewrite
            # of the refusal: one id in, one session out.
            self.assertEqual(
                [ORCHESTRATION_ID],
                sorted(path.name for path in (workspace / "runs" / "orchestrations").iterdir()),
            )
            self.assertTrue((self.session_dir(workspace) / "session.json").is_file())
            # One session, created once. A loser that got far enough to append a
            # second creation event would have left the session's own history
            # claiming something that never happened.
            self.assertEqual(1, self.event_types(workspace).count("session_started"))
            self.assertEqual([], self.work_order_ids(workspace), "start issues no work orders")


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
