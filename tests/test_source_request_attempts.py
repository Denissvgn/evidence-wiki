"""The append-only request-attempt audit and its `record-attempt-failure` command.

A source request that was attempted and produced nothing stays `open`, which says nothing
about whether anyone tried. This audit is what turns "still open" into "attempted twice,
throttled both times", and it is the artifact a controller verifies instead of trusting a
worker's summary. It is append-only on purpose: an attempt that can be erased proves
nothing.
"""

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "workspace-template" / "scripts"
SOURCE_DELIVERY_DOC = REPO_ROOT / "workspace-template" / "docs" / "source-delivery.md"


def load_script_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load script from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REQUESTS = load_script_module("attempt_audit_source_requests", "source_requests.py")
CONTROLLER = load_script_module("attempt_audit_controller", "orchestration_controller.py")
ERRORS = load_script_module("attempt_audit_script_errors", "_script_errors.py")


class AttemptAuditWorkspace(unittest.TestCase):
    """A real initialized workspace with one open request."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "workspace"
        self.run_script(
            "init_research_workspace.py",
            "--target", str(self.workspace),
            "--project-name", "attempt-audit",
            "--project-description", "Request-attempt audit tests.",
        )
        self.request_id = self.add_request("supplier quote for SKU-1")

    def run_script(self, script: str, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / script), *argv],
            capture_output=True,
            text=True,
            check=False,
        )

    def requests_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return self.run_script("source_requests.py", "--project-root", str(self.workspace), *argv)

    def add_request(self, query: str) -> str:
        result = self.requests_cli(
            "add", "--kind", "other", "--query-or-identifier", query, "--rationale", "blocks a question",
            "--format", "json",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)["request"]["request_id"]

    def record(self, *argv: str, request_id: str | None = None) -> subprocess.CompletedProcess:
        return self.requests_cli(
            "record-attempt-failure",
            "--request-id", request_id or self.request_id,
            *argv,
        )

    def config(self) -> dict:
        return REQUESTS.load_config(self.workspace)

    def audit_path(self) -> Path:
        return REQUESTS.request_attempt_audit_path(self.workspace, self.config())

    def events(self) -> list[dict]:
        return REQUESTS.load_attempt_events(self.audit_path())


class RecordAttemptFailureTests(AttemptAuditWorkspace):
    def test_recording_appends_one_event_that_round_trips(self):
        result = self.record(
            "--failure-code", "provider_throttled",
            "--orchestration-id", "orch-1",
            "--action-id", "action-0001",
            "--detail", "connector reported 429",
            "--format", "json",
        )
        self.assertEqual(0, result.returncode, result.stderr)

        report = json.loads(result.stdout)
        self.assertTrue(report["recorded"])
        self.assertEqual("sources/source-request-attempts.jsonl", report["attempts_path"])

        events = self.events()
        self.assertEqual(1, len(events))
        self.assertEqual(report["event"], events[0])
        self.assertEqual(
            {
                "schema_version": "1.0",
                "event_type": "source_request_attempt_failed",
                "request_id": self.request_id,
                "orchestration_id": "orch-1",
                "action_id": "action-0001",
                "failure_code": "provider_throttled",
                "detail": "connector reported 429",
            },
            {key: events[0][key] for key in events[0] if key not in {"event_id", "recorded_at"}},
        )
        self.assertRegex(events[0]["event_id"], r"^attempt-[0-9a-f]{32}$")

    def test_two_attempts_on_one_request_get_distinct_ids(self):
        for index in range(2):
            self.record(
                "--failure-code", "provider_throttled",
                "--orchestration-id", "orch-1",
                "--action-id", f"action-{index}",
            )
        events = self.events()
        self.assertEqual(2, len(events))
        self.assertNotEqual(events[0]["event_id"], events[1]["event_id"])

    def test_the_audit_is_append_only_across_recordings(self):
        for index in range(3):
            result = self.record(
                "--failure-code", "no_result",
                "--orchestration-id", "orch-1",
                "--action-id", f"action-000{index}",
            )
            self.assertEqual(0, result.returncode, result.stderr)

        events = self.events()
        self.assertEqual(3, len(events))
        self.assertEqual(["action-0000", "action-0001", "action-0002"], [e["action_id"] for e in events])
        self.assertEqual(3, len({event["event_id"] for event in events}), "event ids must be distinct")

    def test_the_request_record_is_untouched(self):
        # The failure lives in the audit, not in the request. A router reads "still open"
        # from the store and "attempted twice" from the audit; conflating them would make
        # the store lossy about which attempt is current.
        before = (self.workspace / "sources" / "source-requests.jsonl").read_bytes()
        self.record(
            "--failure-code", "provider_throttled", "--orchestration-id", "orch-1", "--action-id", "a1"
        )
        self.assertEqual(before, (self.workspace / "sources" / "source-requests.jsonl").read_bytes())

    def test_recording_appends_one_log_entry(self):
        self.record("--failure-code", "not_authorized", "--orchestration-id", "orch-1", "--action-id", "a1")
        log = (self.workspace / "log.md").read_text(encoding="utf-8")
        self.assertIn("Recorded a failed acquisition attempt", log)
        self.assertIn(self.request_id, log)
        self.assertIn("not_authorized", log)

    def test_a_long_detail_is_truncated_rather_than_refused(self):
        result = self.record(
            "--failure-code", "http_error",
            "--orchestration-id", "orch-1",
            "--action-id", "a1",
            "--detail", "x" * 5000,
            "--format", "json",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        detail = json.loads(result.stdout)["event"]["detail"]
        self.assertEqual(REQUESTS.MAX_ATTEMPT_DETAIL_LENGTH, len(detail))
        self.assertTrue(detail.endswith("…"))

    def test_an_absent_detail_is_recorded_as_null(self):
        self.record("--failure-code", "no_result", "--orchestration-id", "orch-1", "--action-id", "a1")
        self.assertIsNone(self.events()[0]["detail"])

    def test_every_taxonomy_code_is_accepted(self):
        # Delivery codes are attempt codes too: an attempt that hit a plain HTTP 500 says
        # http_error rather than reaching for a connector-shaped word.
        for index, code in enumerate(REQUESTS.ATTEMPT_FAILURE_CODES):
            with self.subTest(code=code):
                result = self.record(
                    "--failure-code", code, "--orchestration-id", "orch-1", "--action-id", f"a{index}"
                )
                self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(len(REQUESTS.ATTEMPT_FAILURE_CODES), len(self.events()))


class RefusalTests(AttemptAuditWorkspace):
    def assertEnvelope(self, result: subprocess.CompletedProcess, code: str) -> dict:
        self.assertNotEqual(0, result.returncode)
        # Purity: a fatal error is the envelope on stderr with stdout left empty.
        self.assertEqual("", result.stdout)
        envelope = json.loads(result.stderr)
        self.assertEqual(code, envelope["error_code"])
        self.assertTrue(envelope["remediation"].strip())
        return envelope

    def test_an_unknown_request_is_refused(self):
        result = self.record(
            "--failure-code", "no_result", "--orchestration-id", "orch-1", "--action-id", "a1",
            "--format", "json", request_id="req-does-not-exist",
        )
        self.assertEnvelope(result, "REQUEST_UNKNOWN")
        self.assertFalse(self.audit_path().exists(), "a refused attempt must not create the audit")

    def test_a_fulfilled_request_is_refused(self):
        source_id = self.deliver_and_fulfill()
        result = self.record(
            "--failure-code", "no_result", "--orchestration-id", "orch-1", "--action-id", "a1",
            "--format", "json",
        )
        envelope = self.assertEnvelope(result, "REQUEST_ALREADY_FULFILLED")
        self.assertIn(source_id, envelope["message"])
        self.assertEqual([], self.events())

    def test_an_unknown_failure_code_is_refused_with_a_machine_readable_code(self):
        # Deliberately not an argparse `choices` rejection: the caller is a program, and a
        # usage dump on stderr is not something a host can branch on.
        result = self.record(
            "--failure-code", "made_up_code", "--orchestration-id", "orch-1", "--action-id", "a1",
            "--format", "json",
        )
        envelope = self.assertEnvelope(result, "ATTEMPT_FAILURE_CODE_INVALID")
        self.assertIn("made_up_code", envelope["message"])
        for code in REQUESTS.ATTEMPT_FAILURE_CODES:
            self.assertIn(code, envelope["message"])

    def test_a_delivery_only_field_value_is_not_silently_accepted(self):
        result = self.record(
            "--failure-code", "", "--orchestration-id", "orch-1", "--action-id", "a1", "--format", "json"
        )
        self.assertEnvelope(result, "ATTEMPT_FAILURE_CODE_INVALID")

    def test_blank_session_or_action_ids_are_refused(self):
        for label, argv in (
            ("blank orchestration", ("--orchestration-id", "   ", "--action-id", "a1")),
            ("blank action", ("--orchestration-id", "orch-1", "--action-id", "  ")),
        ):
            with self.subTest(case=label):
                result = self.record("--failure-code", "no_result", *argv, "--format", "json")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)

    def test_both_new_codes_have_registered_remediations(self):
        for code in ("REQUEST_ALREADY_FULFILLED", "ATTEMPT_FAILURE_CODE_INVALID"):
            with self.subTest(code=code):
                self.assertIn(code, ERRORS._REMEDIATIONS)
                self.assertTrue(ERRORS.remediation_for(code).strip())

    def deliver_and_fulfill(self) -> str:
        links = self.workspace / "raw" / "links"
        links.mkdir(parents=True, exist_ok=True)
        (links / "quote.txt").write_text("https://example.org/quote\n", encoding="utf-8")
        self.run_script("source_inventory.py", "--project-root", str(self.workspace), "--report")
        manifest = (self.workspace / "sources" / "manifest.jsonl").read_text(encoding="utf-8")
        source_id = json.loads(next(line for line in manifest.splitlines() if line.strip()))["id"]
        result = self.requests_cli(
            "fulfill", "--request-id", self.request_id, "--source-id", source_id, "--format", "json"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return source_id


class ConcurrentAppendTests(AttemptAuditWorkspace):
    def test_concurrent_recordings_all_survive(self):
        # The audit is written under the same lock as request mutation, so interleaved
        # writers cannot lose or corrupt an event. Losing one would let a request look
        # less-attempted than it is, which is the claim the audit exists to disprove.
        results: list[subprocess.CompletedProcess] = []
        lock = threading.Lock()

        def record(index: int) -> None:
            result = self.record(
                "--failure-code", "provider_throttled",
                "--orchestration-id", "orch-1",
                "--action-id", f"action-{index:04d}",
            )
            with lock:
                results.append(result)

        threads = [threading.Thread(target=record, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertTrue(all(result.returncode == 0 for result in results), [r.stderr for r in results])
        events = self.events()
        self.assertEqual(6, len(events))
        self.assertEqual(6, len({event["event_id"] for event in events}))
        self.assertEqual(
            {f"action-{index:04d}" for index in range(6)},
            {event["action_id"] for event in events},
        )


class LockDisciplineTests(AttemptAuditWorkspace):
    def test_the_event_is_appended_while_the_request_lock_is_held(self):
        # What the lock buys, now that event ids are random: the fulfilled-check and the
        # append are atomic with respect to a concurrent `fulfill`. Without it a request
        # could be fulfilled between the two, leaving an attempt failure recorded against
        # evidence that exists. Probed from inside the append, where the lock must be held.
        from types import SimpleNamespace

        lock_path = REQUESTS.source_requests_lock_path(REQUESTS.requests_path(self.workspace, self.config()))
        observed: list[str] = []
        original = REQUESTS.append_attempt_event

        def probing_append(path, event):
            try:
                with REQUESTS.workspace_lock(lock_path, timeout_seconds=0.05, purpose="probe"):
                    observed.append("lock was free")
            except REQUESTS.LockUnavailableError:
                observed.append("lock was held")
            original(path, event)

        REQUESTS.append_attempt_event = probing_append
        self.addCleanup(setattr, REQUESTS, "append_attempt_event", original)

        REQUESTS.run_record_attempt_failure(
            SimpleNamespace(
                project_root=str(self.workspace),
                request_id=self.request_id,
                failure_code="provider_throttled",
                orchestration_id="orch-1",
                action_id="action-0001",
                detail=None,
                format="json",
            )
        )

        self.assertEqual(["lock was held"], observed)
        self.assertEqual(1, len(self.events()))


class EventIdentityTests(unittest.TestCase):
    def test_ids_are_unique_regardless_of_time_or_event_count(self):
        # event_id is the identity the controller fingerprints, so a collision would hide
        # one attempt behind another in the snapshot. The first implementation derived the
        # id from (request, timestamp, observed count) and collided for exactly the case
        # that matters: two attempts on one request inside the same second. Asserted here
        # rather than by timing two subprocesses, which is both weaker and flaky.
        ids = {REQUESTS.generate_attempt_event_id() for _ in range(1000)}
        self.assertEqual(1000, len(ids))
        self.assertTrue(all(re.fullmatch(r"attempt-[0-9a-f]{32}", value) for value in ids))


class ReaderTests(unittest.TestCase):
    """`load_attempt_events` and the aggregation the router consumes."""

    def event(self, **overrides) -> dict:
        base = {
            "schema_version": "1.0",
            "event_type": "source_request_attempt_failed",
            "event_id": "attempt-0000000001",
            "request_id": "req-1",
            "orchestration_id": "orch-1",
            "action_id": "action-0001",
            "failure_code": "no_result",
            "detail": None,
            "recorded_at": "2026-08-08T12:00:00Z",
        }
        base.update(overrides)
        return base

    def write(self, root: Path, events: list[dict]) -> Path:
        path = root / "attempts.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return path

    def test_a_missing_audit_reads_as_no_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual([], REQUESTS.load_attempt_events(Path(tmpdir) / "absent.jsonl"))

    def test_unknown_keys_are_ignored_rather_than_refused(self):
        # Forward compatibility: the event shape is expected to grow, and a reader written
        # today must not refuse events written by a later version of this package.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = self.write(Path(tmpdir), [self.event(scope={"facet_id": "supplier_quote"})])
            events = REQUESTS.load_attempt_events(path)
        self.assertEqual(1, len(events))
        self.assertEqual({"facet_id": "supplier_quote"}, events[0]["scope"])

    def test_the_writer_never_emits_an_unknown_key(self):
        # The other half of the asymmetry: readers are tolerant, writers are strict.
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            subprocess.run(
                [
                    sys.executable, str(SCRIPTS / "init_research_workspace.py"),
                    "--target", str(workspace), "--project-name", "writer-strict",
                    "--project-description", "Writer emits only the specified fields.",
                ],
                capture_output=True, text=True, check=False,
            )
            add = subprocess.run(
                [
                    sys.executable, str(SCRIPTS / "source_requests.py"), "--project-root", str(workspace),
                    "add", "--kind", "other", "--query-or-identifier", "q", "--rationale", "r",
                    "--format", "json",
                ],
                capture_output=True, text=True, check=False,
            )
            request_id = json.loads(add.stdout)["request"]["request_id"]
            subprocess.run(
                [
                    sys.executable, str(SCRIPTS / "source_requests.py"), "--project-root", str(workspace),
                    "record-attempt-failure", "--request-id", request_id, "--failure-code", "no_result",
                    "--orchestration-id", "orch-1", "--action-id", "a1",
                ],
                capture_output=True, text=True, check=False,
            )
            events = REQUESTS.load_attempt_events(
                REQUESTS.request_attempt_audit_path(workspace, REQUESTS.load_config(workspace))
            )
        self.assertEqual(
            {
                "schema_version", "event_type", "event_id", "request_id",
                "orchestration_id", "action_id", "failure_code", "detail", "recorded_at",
            },
            set(events[0]),
        )

    def test_a_malformed_line_is_fatal_rather_than_skipped(self):
        # An audit whose absence is evidence must not silently drop an unreadable event:
        # that would let a request look never-attempted.
        cases = {
            "not json": "{oops\n",
            "not an object": '"just a string"\n',
            "missing required field": json.dumps({"event_id": "attempt-1"}) + "\n",
            "non-string field": json.dumps(self.event(failure_code=7)) + "\n",
        }
        for label, content in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "attempts.jsonl"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(SystemExit):
                    REQUESTS.load_attempt_events(path)

    def test_blank_lines_are_tolerated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "attempts.jsonl"
            path.write_text(json.dumps(self.event()) + "\n\n", encoding="utf-8")
            self.assertEqual(1, len(REQUESTS.load_attempt_events(path)))

    def test_failures_group_by_request_oldest_first(self):
        events = [
            self.event(event_id="attempt-1", request_id="req-1", action_id="a1"),
            self.event(event_id="attempt-2", request_id="req-2", action_id="a2"),
            self.event(event_id="attempt-3", request_id="req-1", action_id="a3"),
        ]
        grouped = REQUESTS.attempt_failures_by_request(events)
        self.assertEqual({"req-1", "req-2"}, set(grouped))
        self.assertEqual(["a1", "a3"], [event["action_id"] for event in grouped["req-1"]])

    def test_the_session_filter_scopes_the_count(self):
        # How a router counts attempts per session: a new session gets a fresh look at
        # every request, which is the supported way to retry after a host-side fix.
        events = [
            self.event(event_id="attempt-1", orchestration_id="orch-a"),
            self.event(event_id="attempt-2", orchestration_id="orch-a"),
            self.event(event_id="attempt-3", orchestration_id="orch-b"),
        ]
        self.assertEqual(3, len(REQUESTS.attempt_failures_by_request(events)["req-1"]))
        self.assertEqual(
            2, len(REQUESTS.attempt_failures_by_request(events, orchestration_id="orch-a")["req-1"])
        )
        self.assertEqual(
            1, len(REQUESTS.attempt_failures_by_request(events, orchestration_id="orch-b")["req-1"])
        )
        self.assertEqual({}, REQUESTS.attempt_failures_by_request(events, orchestration_id="orch-none"))


class ControllerSnapshotTests(AttemptAuditWorkspace):
    """The controller fingerprints the audit the way it fingerprints the candidate audit."""

    def test_a_missing_audit_snapshots_as_empty(self):
        self.assertEqual(
            {},
            CONTROLLER.request_attempt_audit_record_fingerprint_snapshot(self.workspace, self.config()),
        )

    def test_each_event_is_fingerprinted_by_event_id(self):
        self.record("--failure-code", "no_result", "--orchestration-id", "orch-1", "--action-id", "a1")
        self.record("--failure-code", "http_error", "--orchestration-id", "orch-1", "--action-id", "a2")

        snapshot = CONTROLLER.request_attempt_audit_record_fingerprint_snapshot(self.workspace, self.config())
        self.assertEqual({event["event_id"] for event in self.events()}, set(snapshot))

    def test_rewriting_a_recorded_event_changes_its_fingerprint(self):
        # The property the postcondition check depends on: an append-only store where a
        # silent edit is detectable.
        self.record("--failure-code", "no_result", "--orchestration-id", "orch-1", "--action-id", "a1")
        before = CONTROLLER.request_attempt_audit_record_fingerprint_snapshot(self.workspace, self.config())

        path = self.audit_path()
        event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        event["failure_code"] = "not_authorized"
        path.write_text(json.dumps(event) + "\n", encoding="utf-8")

        after = CONTROLLER.request_attempt_audit_record_fingerprint_snapshot(self.workspace, self.config())
        self.assertEqual(set(before), set(after), "the event id is unchanged")
        self.assertNotEqual(before, after, "its content fingerprint is not")

    def test_a_corrupt_audit_refuses_rather_than_reporting_no_attempts(self):
        self.audit_path().parent.mkdir(parents=True, exist_ok=True)
        self.audit_path().write_text("{not json\n", encoding="utf-8")
        with self.assertRaises(CONTROLLER.OrchestrationControllerError) as caught:
            CONTROLLER.request_attempt_audit_record_fingerprint_snapshot(self.workspace, self.config())
        self.assertEqual("ORCHESTRATION_POSTCONDITION_FAILED", caught.exception.error_code)


class DocumentationTests(unittest.TestCase):
    def test_the_audit_contract_is_documented(self):
        text = SOURCE_DELIVERY_DOC.read_text(encoding="utf-8")
        self.assertIn("### Recorded acquisition attempts", text)
        self.assertIn("sources/source-request-attempts.jsonl", text)
        self.assertIn("record-attempt-failure", text)
        for code in ("REQUEST_UNKNOWN", "REQUEST_ALREADY_FULFILLED", "ATTEMPT_FAILURE_CODE_INVALID"):
            with self.subTest(code=code):
                self.assertIn(code, text)

    def test_the_documented_example_matches_what_the_writer_emits(self):
        text = SOURCE_DELIVERY_DOC.read_text(encoding="utf-8")
        match = re.search(r"```json\n(\{\n\s+\"schema_version\": \"1\.0\",\n\s+\"event_type\".*?\n\})", text, re.DOTALL)
        self.assertIsNotNone(match, "docs lost the attempt-event example")
        documented = json.loads(match.group(1))
        self.assertEqual(
            {
                "schema_version", "event_type", "event_id", "request_id",
                "orchestration_id", "action_id", "failure_code", "detail", "recorded_at",
            },
            set(documented),
        )
        self.assertEqual(REQUESTS.ATTEMPT_EVENT_TYPE, documented["event_type"])

    def test_docs_state_the_per_session_and_forward_compatibility_rules(self):
        collapsed = re.sub(r"\s+", " ", SOURCE_DELIVERY_DOC.read_text(encoding="utf-8"))
        self.assertIn("counted **per session**", collapsed)
        self.assertIn("ignore fields they do not recognize", collapsed)


if __name__ == "__main__":
    unittest.main()
