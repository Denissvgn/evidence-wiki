"""Cover ``ws.orchestrate`` -- the embeddable orchestration facade.

The facade wraps a *subprocess* to the workspace's own deployed controller, and
that is the property most worth pinning: the deployed controller is
version-matched to the session state it owns, so an installed library version
must never mutate run state through its own packaged copy. ``ProtocolSeamTests``
asserts the argv actually points into the workspace tree.

Everything else here is about what a long-lived host sees. A session that ended
is an answer rather than an exception; a refusal arrives as a typed error
carrying the controller's own ``error_code``; nothing untyped escapes -- not
``OrchestrationHostError``, not a refusal raised by a workspace script, and
above all not ``SystemExit``, which in an ASGI worker would take the process
down. The end-to-end case drives a real session over a real workspace so those
claims are made against the controller rather than against a mock of it.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from evidence_wiki import _contract, errors, orchestration  # noqa: E402
from evidence_wiki._facades import orchestrate as facade  # noqa: E402
from evidence_wiki.workspace import Workspace  # noqa: E402

QUESTION_SLUG = "benchmarks"
AGENT_ID = "agent-library"
ORCHESTRATION_ID = "orch-library"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INIT = load_script_module("library_orchestrate_init", "init_research_workspace.py")
INTAKE = load_script_module("library_orchestrate_intake", "intake_questions.py")
CLAIM = load_script_module("library_orchestrate_claim", "question_claim.py")
REQUESTS = load_script_module("library_orchestrate_requests", "source_requests.py")
RESOLVE = load_script_module("library_orchestrate_resolve", "question_resolve.py")
LOCKS = load_script_module("library_orchestrate_locks", "_workspace_locks.py")


def completed(returncode: int, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["controller"], returncode=returncode, stdout=stdout, stderr=stderr)


def envelope(error_code: str, message: str) -> str:
    return json.dumps(
        {
            "schema_version": "1.0",
            "error_code": error_code,
            "message": message,
            "recoverable": True,
            "remediation": "Fix it and retry.",
        }
    )


def work_order_document(orchestration_id: str = ORCHESTRATION_ID, **overrides) -> dict:
    document = {
        "schema_version": "1.0",
        "artifact_type": "orchestration_work_order",
        "orchestration_id": orchestration_id,
        "action_id": "action-0001",
        "issued_at": "2026-08-10T00:00:00Z",
        "phase": "research",
        "skill": "research-run",
        "run_id": "run-1",
        "agent_id": AGENT_ID,
        "scope": {"question_slugs": [QUESTION_SLUG], "request_ids": [], "candidate_ids": []},
        "provider_policy": {
            "discovery": {"enabled": False, "providers": []},
            "acquisition": {"enabled": False, "providers": []},
        },
        "budgets": {"action_timeout_seconds": 60},
        "inputs": ["wiki/questions/benchmarks.md"],
        "required_postconditions": [{"check": "child_run_state", "expected": "answering"}],
        "lease": {"duration_seconds": 60, "expires_at": "2099-08-10T00:01:00Z", "attempt": 1},
    }
    document.update(overrides)
    return document


class WorkspaceBuilder:
    """Workspace scaffolding shared by the suites below.

    Deliberately not a ``TestCase``: subclassing one that carries tests would
    re-run the whole parent suite under every child class.

    The workspace scripts run *in process* from the repository template, whose
    sibling loader resolves against the template directory. Nothing here writes
    bytecode into the created workspace's ``scripts/``, which the controller's
    trusted-input fingerprint would otherwise report as drift mid-action.
    """

    def run_module(self, module, argv: list[str]) -> dict:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = module.main(argv)
        self.assertEqual(0, int(code or 0), stderr.getvalue() or stdout.getvalue())
        payload = stdout.getvalue().strip()
        return json.loads(payload) if payload.startswith(("{", "[")) else {}

    def init_workspace(self, root: Path, *, question: bool = True) -> Path:
        target = root / "workspace"
        self.run_module(
            INIT,
            [
                "--target",
                str(target),
                "--project-name",
                "library-orchestrate",
                "--project-description",
                "Workspace for the embeddable orchestration facade.",
            ],
        )
        if question:
            batch = root / "batch.json"
            batch.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "questions": [
                            {
                                "id": QUESTION_SLUG,
                                "question": "Which benchmarks matter for this decision?",
                                "priority": "high",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.run_module(INTAKE, ["--project-root", str(target), "--from-file", str(batch), "--format", "json"])
        return target

    def block_the_question(self, target: Path) -> str:
        """Satisfy the research postcondition with no evidence and no network.

        Claiming the question and blocking it on an open source request is the
        one terminal outcome reachable offline, and it is what makes ``submit``
        a real verification rather than a formality: the controller reads the
        question page, not the result document's claim about it.
        """
        self.run_module(
            CLAIM,
            ["--project-root", str(target), "claim", "--slug", QUESTION_SLUG, "--agent-id", AGENT_ID, "--format", "json"],
        )
        request = self.run_module(
            REQUESTS,
            [
                "--project-root", str(target), "add", "--kind", "paper",
                "--query-or-identifier", "Evidence needed for the benchmark question",
                "--rationale", "This run supplies no evidence.",
                "--priority", "high", "--question-slug", QUESTION_SLUG, "--format", "json",
            ],
        )["request"]["request_id"]
        self.run_module(
            RESOLVE,
            [
                "--project-root", str(target), "block", "--slug", QUESTION_SLUG, "--agent-id", AGENT_ID,
                "--blocked-reason", "This run supplies no evidence.",
                "--request-id", request, "--format", "json",
            ],
        )
        return request

    def result_document(self, action_id: str, *, outcome: str = "completed") -> dict:
        return {
            "schema_version": "1.0",
            "action_id": action_id,
            "outcome": outcome,
            "summary": "Blocked the question on an open source request.",
            "artifacts": [],
        }


class FacadeSessionTests(WorkspaceBuilder, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_a_full_session_runs_start_next_submit_and_status_in_process(self):
        """The hot path the change request was filed about, end to end.

        Every call here spawns the workspace's deployed controller; what the
        facade removes is the CLI process around it, not the controller.
        """
        target = self.init_workspace(self.tmp)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            self.assertEqual(ORCHESTRATION_ID, session.orchestration_id)

            order = session.next()
            self.assertEqual("orchestration_work_order", order["artifact_type"])
            self.assertEqual("research", order["phase"])
            self.assertEqual(ORCHESTRATION_ID, order["orchestration_id"])

            self.block_the_question(target)
            submitted = session.submit(
                order["action_id"], self.result_document(order["action_id"]), agent_id=AGENT_ID
            )
            self.assertEqual("orchestration_session", submitted["artifact_type"])
            self.assertEqual(order["action_id"], submitted["last_completed_action_id"])

            finished = session.next()

        self.assertEqual("orchestration_session", finished["artifact_type"])
        self.assertEqual("blocked_on_sources", finished["status"])

    def test_a_terminal_session_is_returned_rather_than_raised(self):
        """A session that ended is an answer.

        The controller signals `blocked_on_sources` with a non-zero exit, and a
        host looping on ``next`` must see a document rather than an exception --
        otherwise every normal end-of-session becomes an error path.
        """
        target = self.init_workspace(self.tmp)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            order = session.next()
            self.block_the_question(target)
            session.submit(order["action_id"], self.result_document(order["action_id"]), agent_id=AGENT_ID)
            session.next()

            terminal_next = session.next()
            terminal_status = session.status()

        self.assertEqual("blocked_on_sources", terminal_next["status"])
        self.assertEqual("blocked_on_sources", terminal_status["status"])
        self.assertEqual("blocked_on_sources", terminal_status["verdict"])

    def test_a_dict_result_round_trips_and_leaves_no_temporary_file_behind(self):
        target = self.init_workspace(self.tmp)
        observed: list[Path] = []
        real_submit = orchestration.protocol_submit

        def recording_submit(root, orchestration_id, action_id, result_file, **kwargs):
            observed.append(Path(result_file))
            return real_submit(root, orchestration_id, action_id, result_file, **kwargs)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            order = session.next()
            self.block_the_question(target)
            with mock.patch.object(orchestration, "protocol_submit", side_effect=recording_submit):
                submitted = session.submit(
                    order["action_id"], self.result_document(order["action_id"]), agent_id=AGENT_ID
                )

        self.assertEqual(order["action_id"], submitted["last_completed_action_id"])
        self.assertEqual(1, len(observed))
        self.assertFalse(observed[0].exists(), "the result temporary file outlived the call")
        # Never inside the workspace: a scratch file under the tree is drift the
        # controller's own integrity guards would report.
        self.assertNotIn(target.resolve(), observed[0].resolve().parents)

    def test_submit_accepts_a_path_to_an_existing_result_file(self):
        target = self.init_workspace(self.tmp)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            order = session.next()
            self.block_the_question(target)
            path = self.tmp / "result.json"
            path.write_text(json.dumps(self.result_document(order["action_id"])), encoding="utf-8")

            submitted = session.submit(order["action_id"], path, agent_id=AGENT_ID)

        self.assertEqual(order["action_id"], submitted["last_completed_action_id"])
        self.assertTrue(path.exists(), "a caller-owned result file must not be removed")

    def test_submit_refuses_a_result_that_is_neither_a_mapping_nor_an_existing_file(self):
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with self.assertRaises(errors.UsageError) as missing:
                session.submit("action-0001", self.tmp / "absent.json")
            with self.assertRaises(errors.UsageError) as wrong_type:
                session.submit("action-0001", 17)

        self.assertEqual("VALUE_INVALID", missing.exception.error_code)
        self.assertEqual("VALUE_INVALID", wrong_type.exception.error_code)

    def test_naming_a_session_performs_no_io(self):
        """A host restarting with a hundred live sessions should not pay a
        hundred controller spawns to reconstruct its drivers."""
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws, mock.patch.object(orchestration, "_invoke_controller") as invoke:
            session = ws.orchestrate.session(ORCHESTRATION_ID)

        invoke.assert_not_called()
        self.assertEqual(ORCHESTRATION_ID, session.orchestration_id)

    def test_naming_a_session_refuses_an_empty_id(self):
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            with self.assertRaises(errors.UsageError) as caught:
                ws.orchestrate.session("   ")

        self.assertEqual("VALUE_INVALID", caught.exception.error_code)

    def test_every_operation_refuses_once_the_handle_is_closed(self):
        target = self.init_workspace(self.tmp, question=False)
        ws = Workspace.open(target)
        session = ws.orchestrate.session(ORCHESTRATION_ID)
        ws.close()

        for label, call in (
            ("start", lambda: ws.orchestrate.start(AGENT_ID)),
            ("next", session.next),
            ("status", session.status),
            ("submit", lambda: session.submit("action-0001", {"schema_version": "1.0"})),
        ):
            with self.subTest(operation=label), self.assertRaises(errors.ConfigError) as caught:
                call()
            self.assertEqual("WORKSPACE_UNREADABLE", caught.exception.error_code)


class FacadeErrorTranslationTests(WorkspaceBuilder, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_workspace_unsafe_surfaces_as_a_typed_error_with_its_code(self):
        """A question page that is not a singly linked regular file trips the
        controller's integrity guard; the host sees the code, not stderr."""
        target = self.init_workspace(self.tmp)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            (target / "wiki" / "questions" / "linked.md").symlink_to(Path("..") / ".." / "research.yml")

            with self.assertRaises(errors.OrchestrationError) as caught:
                session.next()

        self.assertEqual("ORCHESTRATION_WORKSPACE_UNSAFE", caught.exception.error_code)
        self.assertIn("integrity guard", str(caught.exception))
        self.assertNotIsInstance(caught.exception, orchestration.OrchestrationHostError)

    def test_the_per_session_driver_lock_surfaces_as_a_typed_driver_busy_error(self):
        """Two drivers, one session: the second is refused, not silently queued.

        The lock is the workspace's own per-session mutation lock, taken by the
        deployed controller around ``next``. Holding it while a second driver
        calls is what concurrent drivers look like from inside the first one's
        window.

        CR-8 made that refusal specific. It used to arrive as the generic
        ``LOCK_UNAVAILABLE`` -- the same code a workspace with no usable lock
        backend at all reports -- so a host could not tell "retry in a moment"
        from "this filesystem will never support locking". ``ORCHESTRATION_DRIVER_BUSY``
        names contention and nothing else, and carries the holder that caused it.
        ``LOCK_UNAVAILABLE`` still means what it always meant; it just no longer
        means two things.
        """
        target = self.init_workspace(self.tmp)
        lock_path = target / "runs" / "orchestrations" / ORCHESTRATION_ID / ".locks" / "session.lock"

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            held, release = threading.Event(), threading.Event()

            def hold_the_session_lock():
                with LOCKS.workspace_lock(
                    lock_path,
                    purpose="competing driver",
                    holder={"agent_id": "competing-driver", "pid": 4242},
                ):
                    held.set()
                    release.wait(120)

            holder = threading.Thread(target=hold_the_session_lock, daemon=True)
            holder.start()
            self.addCleanup(holder.join, 30)
            self.addCleanup(release.set)
            self.assertTrue(held.wait(30), "the competing driver never acquired the session lock")

            with self.assertRaises(errors.OrchestrationError) as caught:
                session.next()

        self.assertEqual("ORCHESTRATION_DRIVER_BUSY", caught.exception.error_code)
        self.assertTrue(caught.exception.recoverable)
        # Classification comes from the envelope, not the child's exit status: the
        # controller's own ``EXIT_DRIVER_BUSY`` (5) is asserted where processes are
        # observable, in the controller suite. Here the point is that a non-zero exit
        # the facade has never seen before does not divert this into a host error.
        self.assertNotIsInstance(caught.exception, orchestration.OrchestrationHostError)
        # The holder block survives the JSON envelope the subprocess boundary
        # forces it through, so a host can report *who* is busy, not just that
        # someone is.
        self.assertEqual("competing-driver", caught.exception.details["holder"]["agent_id"])
        self.assertEqual(4242, caught.exception.details["holder"]["pid"])
        self.assertEqual(ORCHESTRATION_ID, caught.exception.details["orchestration_id"])

    def test_a_waiting_driver_outlasts_the_holder_instead_of_being_refused(self):
        """``driver_wait_seconds`` restores queueing for a library host too.

        The refusal default is what makes interleaving loud, but CR-8's own
        premise is that *the host* decides whether to wait or fail. That was
        true only for shell callers until this parameter existed: the facade
        passed no wait and every embedding host got the immediate refusal
        whether it wanted one or not.

        The competing driver is released on a timer rather than by the call
        under test, so the wait is genuinely satisfied by the holder going away
        -- not by the lock having been free all along. The preceding refusal is
        what proves the lock was actually held when the waiting call started.
        """
        target = self.init_workspace(self.tmp)
        lock_path = target / "runs" / "orchestrations" / ORCHESTRATION_ID / ".locks" / "session.lock"

        with Workspace.open(target) as ws:
            session = ws.orchestrate.start(AGENT_ID, orchestration_id=ORCHESTRATION_ID)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            held, release = threading.Event(), threading.Event()

            def hold_the_session_lock():
                with LOCKS.workspace_lock(
                    lock_path,
                    purpose="competing driver",
                    holder={"agent_id": "competing-driver", "pid": 4242},
                ):
                    held.set()
                    release.wait(120)

            holder = threading.Thread(target=hold_the_session_lock, daemon=True)
            holder.start()
            self.addCleanup(holder.join, 30)
            self.addCleanup(release.set)
            self.assertTrue(held.wait(30), "the competing driver never acquired the session lock")

            # Held right now: without a wait this is a refusal, every time.
            with self.assertRaises(errors.OrchestrationError) as refused:
                session.next()
            self.assertEqual("ORCHESTRATION_DRIVER_BUSY", refused.exception.error_code)

            # Hand the lock back shortly after the waiting call has started, so
            # the call must actually block and then proceed.
            threading.Timer(0.5, release.set).start()
            work_order = session.next(driver_wait_seconds=60)

        # A work order, not a refusal: the wait was honoured end to end, through
        # the facade, the argv seam, and the deployed controller's own lock.
        self.assertEqual(ORCHESTRATION_ID, work_order["orchestration_id"])

    def test_an_omitted_wait_sends_no_flag_at_all(self):
        """The default must be the controller's, not one this package restates.

        Every limit on this seam is omitted from argv when ``None`` so a newer
        deployed controller can move its own default. Asserting the *absence* of
        the flag is the only way to catch a well-meaning ``or 0.0`` that would
        pin today's default into the library forever -- and it is also what keeps
        this call byte-identical against a workspace old enough not to know the
        flag at all.
        """
        target = self.init_workspace(self.tmp, question=False)
        seen: list[list[str]] = []

        def capture(root, command, arguments):
            seen.append(list(arguments))
            return completed(0, stdout=json.dumps({"artifact_type": "orchestration_session"}))

        with Workspace.open(target) as ws, mock.patch.object(
            orchestration, "_invoke_controller", side_effect=capture
        ):
            ws.orchestrate.session(ORCHESTRATION_ID).next()
            ws.orchestrate.session(ORCHESTRATION_ID).next(driver_wait_seconds=30)

        self.assertNotIn("--driver-wait-seconds", seen[0])
        self.assertIn("--driver-wait-seconds", seen[1])
        self.assertEqual("30.0", seen[1][seen[1].index("--driver-wait-seconds") + 1])

    def test_an_unusable_wait_is_refused_before_a_controller_is_spawned(self):
        """A hang requested by argument is still a hang; refuse it locally.

        ``nan`` is the case that earns a local check rather than deferring to
        the controller: it renders into argv as the literal ``nan``, so the
        failure would otherwise surface as an argparse usage error from a
        subprocess instead of a typed refusal naming the argument.
        """
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws, mock.patch.object(
            orchestration, "_invoke_controller"
        ) as invoked:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            for value in (-1, float("inf"), float("nan"), "30"):
                with self.subTest(driver_wait_seconds=value):
                    with self.assertRaises(errors.UsageError) as caught:
                        session.next(driver_wait_seconds=value)
                    self.assertEqual("VALUE_INVALID", caught.exception.error_code)
                    self.assertIn("driver_wait_seconds", str(caught.exception))
            invoked.assert_not_called()

    def test_a_controller_that_died_without_an_envelope_still_yields_a_typed_error(self):
        """No envelope is an absence, not a second failure to report."""
        target = self.init_workspace(self.tmp, question=False)
        crash = completed(1, stderr='Traceback (most recent call last):\n  IsADirectoryError: 21\n')

        with Workspace.open(target) as ws, mock.patch.object(
            orchestration, "_invoke_controller", return_value=crash
        ):
            with self.assertRaises(errors.OrchestrationError) as caught:
                ws.orchestrate.session(ORCHESTRATION_ID).status()

        self.assertEqual(facade.HOST_ERROR_CODE, caught.exception.error_code)
        self.assertIn("IsADirectoryError", str(caught.exception))
        self.assertEqual(1, caught.exception.exit_code)

    def test_child_output_is_redacted_before_it_reaches_the_exception(self):
        target = self.init_workspace(self.tmp, question=False)
        secret = "s3cr3t-value-from-the-environment"
        leaky = completed(2, stderr=f"controller crashed while using {secret}\n")
        leaky_envelope = completed(2, stderr=envelope("ORCHESTRATION_STATE_INVALID", f"state names {secret}"))

        with mock.patch.dict(os.environ, {"EVIDENCE_WIKI_TEST_TOKEN": secret}), Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "_invoke_controller", return_value=leaky):
                with self.assertRaises(errors.OrchestrationError) as plain:
                    session.status()
            with mock.patch.object(orchestration, "_invoke_controller", return_value=leaky_envelope):
                with self.assertRaises(errors.OrchestrationError) as enveloped:
                    session.status()

        for label, caught in (("plain", plain), ("envelope", enveloped)):
            with self.subTest(shape=label):
                self.assertNotIn(secret, str(caught.exception))
                self.assertIn("<redacted>", str(caught.exception))
        self.assertEqual("ORCHESTRATION_STATE_INVALID", enveloped.exception.error_code)

    def test_the_untyped_host_error_never_escapes_the_facade(self):
        target = self.init_workspace(self.tmp, question=False)
        host_error = orchestration.OrchestrationHostError("host refused before the child ran", exit_code=5)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            for operation, patched in (("next", "protocol_next"), ("status", "protocol_status")):
                with self.subTest(operation=operation), mock.patch.object(
                    orchestration, patched, side_effect=host_error
                ):
                    with self.assertRaises(errors.EvidenceWikiError) as caught:
                        getattr(session, operation)()
                    self.assertNotIsInstance(caught.exception, orchestration.OrchestrationHostError)
                    self.assertEqual(facade.HOST_ERROR_CODE, caught.exception.error_code)
                    self.assertEqual(5, caught.exception.exit_code)

    def test_system_exit_never_reaches_the_caller(self):
        """A library that let ``SystemExit`` through would end an ASGI worker."""
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "protocol_next", side_effect=SystemExit("refused: no session")):
                with self.assertRaises(errors.OrchestrationError) as caught:
                    session.next()

        self.assertEqual(facade.EXITED_ERROR_CODE, caught.exception.error_code)
        self.assertIn("no session", str(caught.exception))

    def test_a_result_that_cannot_be_staged_still_refuses_in_type(self):
        """Both ways staging a dict result can fail land on a typed error.

        The temporary file is created before the controller is reachable, so a
        failure there is on this side of the boundary and would otherwise escape
        as a bare ``OSError`` or ``TypeError``.
        """
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with self.assertRaises(errors.UsageError) as unserializable:
                session.submit("action-0001", {"artifacts": {object()}})
            with mock.patch.object(facade.tempfile, "mkstemp", side_effect=OSError("no space left on device")):
                with self.assertRaises(errors.OrchestrationError) as unstageable:
                    session.submit("action-0001", self.result_document("action-0001"))

        self.assertEqual("VALUE_INVALID", unserializable.exception.error_code)
        self.assertEqual(facade.HOST_ERROR_CODE, unstageable.exception.error_code)
        self.assertIn("no space left", str(unstageable.exception))

    def test_a_keyboard_interrupt_is_left_alone(self):
        """The ``SystemExit`` guard must not also make a host unkillable."""
        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "protocol_status", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    session.status()

    def test_a_refusal_is_recognized_by_shape_rather_than_by_class(self):
        """The module loader gives every loaded script its own ``ScriptRefusal``
        class object, so package-side ``except ScriptRefusal`` would catch
        nothing. This refusal shares no ancestry with anything the package
        imported, and must still translate."""

        class ForeignRefusal(Exception):
            error_code = "CLAIM_HELD"

            def to_envelope(self):
                return {
                    "schema_version": "1.0",
                    "error_code": self.error_code,
                    "message": "another agent holds the claim",
                    "recoverable": False,
                    "remediation": "Wait for the holder or steal a stale claim.",
                }

        target = self.init_workspace(self.tmp, question=False)

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "protocol_next", side_effect=ForeignRefusal()):
                with self.assertRaises(errors.ClaimError) as caught:
                    session.next()

        self.assertEqual("CLAIM_HELD", caught.exception.error_code)
        self.assertFalse(caught.exception.recoverable)
        self.assertNotIsInstance(caught.exception, ForeignRefusal)

    def test_next_validates_the_work_order_the_way_the_managed_runner_does(self):
        target = self.init_workspace(self.tmp, question=False)
        unsafe = work_order_document(inputs=["/etc/passwd"])

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "protocol_next", return_value=unsafe):
                with self.assertRaises(errors.OrchestrationError) as caught:
                    session.next()

        self.assertIn("workspace-relative", str(caught.exception))

    def test_a_work_order_issued_for_another_session_is_refused(self):
        target = self.init_workspace(self.tmp, question=False)
        stray = work_order_document(orchestration_id="orch-somebody-else")

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            with mock.patch.object(orchestration, "protocol_next", return_value=stray):
                with self.assertRaises(errors.OrchestrationError) as caught:
                    session.next()

        self.assertIn("does not belong to the active orchestration", str(caught.exception))


class ProtocolSeamTests(unittest.TestCase):
    """The seams in :mod:`evidence_wiki.orchestration` the facade is built on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "orchestration_controller.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (self.root / "research.yml").write_text("project: {name: seam}\n", encoding="utf-8")

    def invoked_argv(self, call) -> list[str]:
        """Return the argv one seam handed to ``subprocess.run``.

        Patched at ``subprocess.run`` rather than at ``_invoke_controller`` on
        purpose: argv construction is exactly what is under test here.
        """
        session = {"artifact_type": "orchestration_session", "orchestration_id": ORCHESTRATION_ID}
        with mock.patch.object(
            orchestration.subprocess, "run", return_value=completed(0, stdout=json.dumps(session))
        ) as run:
            call()
        return list(run.call_args.args[0])

    def test_every_seam_spawns_the_workspaces_own_deployed_controller(self):
        """The architectural rule this unit must not break.

        The deployed controller is authoritative for run-state mutation because
        it is version-matched to the session state it owns. Reaching for the
        copy packaged with this distribution would let an installed library
        version mutate run state owned by a different workspace version.
        """
        deployed = str(self.root / "scripts" / "orchestration_controller.py")
        calls = {
            "start": lambda: orchestration.protocol_start(self.root, AGENT_ID),
            "next": lambda: orchestration.protocol_next(self.root, ORCHESTRATION_ID),
            "submit": lambda: orchestration.protocol_submit(self.root, ORCHESTRATION_ID, "action-0001", "/tmp/r.json"),
            "status": lambda: orchestration.protocol_status(self.root, orchestration_id=ORCHESTRATION_ID),
        }
        for command, call in calls.items():
            with self.subTest(command=command):
                argv = self.invoked_argv(call)
                self.assertIn(deployed, argv)
                self.assertNotIn(str(SRC_ROOT), " ".join(argv))
                self.assertEqual([command], [item for item in argv if item == command])
                self.assertEqual(["--format", "json"], argv[-2:])

    def test_omitted_limits_are_left_to_the_deployed_controller(self):
        """This package must not pin a default a newer workspace has moved on
        from, so an unspecified limit is simply absent from argv."""
        bare = self.invoked_argv(lambda: orchestration.protocol_start(self.root, AGENT_ID))
        explicit = self.invoked_argv(
            lambda: orchestration.protocol_start(
                self.root,
                AGENT_ID,
                orchestration_id=ORCHESTRATION_ID,
                max_actions=3,
                action_timeout_seconds=30,
                total_timeout_seconds=300,
            )
        )

        for option in ("--orchestration-id", "--max-actions", "--action-timeout-seconds", "--total-timeout-seconds"):
            with self.subTest(option=option):
                self.assertNotIn(option, bare)
                self.assertIn(option, explicit)
        self.assertEqual("3", explicit[explicit.index("--max-actions") + 1])

    def test_next_forwards_resume_only_when_asked(self):
        without = self.invoked_argv(lambda: orchestration.protocol_next(self.root, ORCHESTRATION_ID))
        with_resume = self.invoked_argv(
            lambda: orchestration.protocol_next(self.root, ORCHESTRATION_ID, agent_id=AGENT_ID, resume=True)
        )

        self.assertNotIn("--resume", without)
        self.assertIn("--resume", with_resume)
        self.assertEqual(AGENT_ID, with_resume[with_resume.index("--agent-id") + 1])

    def test_a_terminal_or_paused_session_is_returned_on_a_non_zero_exit(self):
        for status, exit_code in (("blocked_on_sources", 3), ("complete", 0), ("paused", 4), ("no_ship", 2)):
            with self.subTest(status=status):
                payload = {
                    "artifact_type": "orchestration_session",
                    "orchestration_id": ORCHESTRATION_ID,
                    "status": status,
                }
                with mock.patch.object(
                    orchestration, "_invoke_controller", return_value=completed(exit_code, stdout=json.dumps(payload))
                ):
                    self.assertEqual(payload, orchestration.protocol_next(self.root, ORCHESTRATION_ID))

    def test_a_non_session_payload_on_a_non_zero_exit_still_refuses(self):
        order = completed(2, stdout=json.dumps(work_order_document()), stderr=envelope("ORCHESTRATION_STATE_INVALID", "no"))

        with mock.patch.object(orchestration, "_invoke_controller", return_value=order):
            with self.assertRaises(orchestration.OrchestrationHostError) as caught:
                orchestration.protocol_next(self.root, ORCHESTRATION_ID)

        self.assertEqual("ORCHESTRATION_STATE_INVALID", caught.exception.envelope["error_code"])

    def test_a_controller_failure_carries_its_envelope_for_in_process_callers(self):
        refusal = completed(2, stderr=envelope("ORCHESTRATION_UNKNOWN", "unknown orchestration id: orch-x"))

        with mock.patch.object(orchestration, "_invoke_controller", return_value=refusal):
            with self.assertRaises(orchestration.OrchestrationHostError) as caught:
                orchestration.protocol_status(self.root, orchestration_id="orch-x")

        self.assertEqual("ORCHESTRATION_UNKNOWN", caught.exception.envelope["error_code"])
        self.assertIs(errors.OrchestrationError, type(errors.error_from_envelope(caught.exception.envelope)))

    def test_a_traceback_leaves_no_envelope_rather_than_a_fabricated_one(self):
        crash = completed(1, stderr="Traceback (most recent call last):\n")

        with mock.patch.object(orchestration, "_invoke_controller", return_value=crash):
            with self.assertRaises(orchestration.OrchestrationHostError) as caught:
                orchestration.protocol_status(self.root)

        self.assertIsNone(caught.exception.envelope)


class PublishedSurfaceTests(WorkspaceBuilder, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_the_contract_names_resolve_to_callables_on_the_facade(self):
        target = self.init_workspace(self.tmp, question=False)
        declared = [name for name in _contract.LIBRARY_API_SURFACE if name.startswith("orchestrate.")]

        with Workspace.open(target) as ws:
            session = ws.orchestrate.session(ORCHESTRATION_ID)
            resolved = {
                "orchestrate.start": ws.orchestrate.start,
                "orchestrate.session.next": session.next,
                "orchestrate.session.submit": session.submit,
                "orchestrate.session.status": session.status,
            }

        self.assertEqual(sorted(resolved), sorted(declared))
        for name, attribute in resolved.items():
            with self.subTest(name=name):
                self.assertTrue(callable(attribute))


if __name__ == "__main__":
    unittest.main()
