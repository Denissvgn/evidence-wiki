#!/usr/bin/env python3
"""Retain frozen comparative assignments and bounded host-observed attempts."""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._comparative_protocol import (  # noqa: E402
    final_judgment_packet,
    freeze_plan,
    generator_packet,
    normalize_answer,
    project_report,
    validate_plan,
)
from tools._journey_cases import canonical, digest  # noqa: E402
from tools._qualification_process import stop_process  # noqa: E402

MAX_EVENT_BYTES = 1_048_576
MAX_LEDGER_BYTES = 16_777_216
MAX_EVENTS = 2048
MAX_EXECUTABLE_SLOTS = 18


def clock():
    return datetime.now(timezone.utc).isoformat()


def command_manifest(command):
    """Pin selected executable/script bytes, without claiming dependency isolation."""
    rows = []
    for index, item in enumerate(command):
        selected = shutil.which(item) if index == 0 else item
        if selected is None:
            raise ValueError("trial_command_unavailable")
        path = Path(selected)
        try:
            is_file = path.is_file()
        except OSError as error:
            # Inline command text cannot name a file when the native path bound
            # is exceeded. Other filesystem errors still invalidate custody.
            if index == 0 or error.errno != errno.ENAMETOOLONG:
                raise
            is_file = False
        if not is_file:
            if index == 0:
                raise ValueError("trial_command_unavailable")
            continue
        path = path.resolve(strict=True)
        info = path.stat()
        if info.st_size > 33_554_432:
            raise ValueError("trial_command_file_bound")
        raw = path.read_bytes()
        after = path.stat()
        if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
            raise ValueError("trial_command_file_changed")
        rows.append({"argument_index": index, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    return rows


def checked_directory(path):
    if os.name != "posix":
        raise ValueError("trial_platform_unsupported")
    path = Path(path).absolute()
    if path.resolve() != path or path.is_symlink():
        raise ValueError("trial_directory_not_canonical")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("trial_directory_not_private")
    return path


def read_private(path, maximum):
    if os.name != "posix":
        raise ValueError("trial_platform_unsupported")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > maximum):
            raise ValueError("trial_file_not_private_bounded_regular")
        data = b""
        while chunk := os.read(descriptor, min(65536, maximum + 1 - len(data))):
            data += chunk
            if len(data) > maximum:
                raise ValueError("trial_file_changed_or_overflow")
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (len(data) > maximum or any(getattr(os.fstat(descriptor), field) != getattr(info, field) for field in fields)
                or any(getattr(os.stat(path, follow_symlinks=False), field) != getattr(info, field) for field in fields)):
            raise ValueError("trial_file_changed_or_overflow")
        return data
    finally:
        os.close(descriptor)


def publish_new(path, value):
    data = canonical(value)
    if len(data) > MAX_EVENT_BYTES:
        raise ValueError("trial_document_overflow")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def replace_private(path, value):
    """Publish one bounded signed host anchor; parent directory remains private."""
    descriptor, temporary = tempfile.mkstemp(prefix=".trial-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class TrialLedger:
    """Host-private append-only attempt evidence; native research stays separate.

    HMACs authenticate host retention, not model execution or scientific truth.
    Same-user workers and process groups are not a malicious-worker OS sandbox.
    Final comparison judging is available only after immutable generation seal.
    """

    def __init__(self, directory, plan, *, observer_key, work_root, create=False, expected_head=None,
                 read_only=False):
        if type(read_only) is not bool or read_only and create:
            raise ValueError("trial_read_only_selection_invalid")
        if os.name != "posix":
            raise ValueError("trial_platform_unsupported")
        self._read_only = read_only
        if type(observer_key) is not bytes or not 32 <= len(observer_key) <= 128:
            raise ValueError("trial_observer_key_invalid")
        self.plan = validate_plan(plan)
        if len(self.plan["schedule"]) > MAX_EXECUTABLE_SLOTS:
            raise ValueError("trial_host_slot_capacity_exceeded")
        self.key = observer_key
        self._directory_identity = None
        self._lock_thread = None
        self._lock_identity = None
        self._expected_head = expected_head
        if not create and expected_head is None:
            raise ValueError("trial_restore_requires_trusted_latest_head")
        directory, work_root = Path(directory).absolute(), Path(work_root).absolute()
        if (work_root.is_relative_to(ROOT) or ROOT.is_relative_to(work_root)
                or work_root.is_relative_to(directory) or directory.is_relative_to(work_root)):
            raise ValueError("trial_workspaces_must_be_separate")
        if directory.is_relative_to(ROOT):
            subprocess.run(["git", "check-ignore", "-q", "--", str(directory / "plan.json")],  # noqa: S603,S607 -- fixed read-only repository policy command.
                cwd=ROOT, check=True, capture_output=True)
        if create:
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            work_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.directory = checked_directory(directory)
        self.work_root = checked_directory(work_root)
        self._directory_identity = [self.directory.stat().st_dev, self.directory.stat().st_ino]
        self.binding = {"plan": self.plan, "observer_id": hashlib.sha256(observer_key).hexdigest(),
            "work_root": str(self.work_root), "work_root_identity": [self.work_root.stat().st_dev, self.work_root.stat().st_ino]}
        if create:
            publish_new(directory / "plan.json", self.binding)
            descriptor = os.open(directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            publish_new(directory / "head.json", self._signed({"sequence": 0, "bytes": 0,
                "event_sha256": None, "plan_sha256": self.plan["plan_sha256"]}))
        stored = json.loads(read_private(directory / "plan.json", MAX_EVENT_BYTES))
        if stored != self.binding:
            raise ValueError("trial_binding_changed")
        with self.locked():
            if not self._read_only:
                self._recover_append()
            self.events()
        self._expected_head = digest(self._head())

    def _signed(self, value):
        result = {**value, "authentication": None}
        result["authentication"] = hmac.new(self.key, canonical(result), hashlib.sha256).hexdigest()
        return result

    def _verify(self, value):
        if (type(value) is not dict or type(value.get("authentication")) is not str
                or not hmac.compare_digest(self._signed(value)["authentication"], value["authentication"])):
            raise ValueError("trial_anchor_authentication_failed")
        return value

    def _head(self, *, enforce_expected=True):
        value = self._verify(json.loads(read_private(self.directory / "head.json", 4096)))
        if set(value) != {"sequence", "bytes", "event_sha256", "plan_sha256", "authentication"} or value["plan_sha256"] != self.plan["plan_sha256"]:
            raise ValueError("trial_head_invalid")
        if enforce_expected and self._expected_head is not None and digest(value) != self._expected_head:
            raise ValueError("trial_trusted_latest_head_mismatch")
        return value

    def _recover_append(self):
        pending = self.directory / "pending.json"
        if self._read_only:
            raise ValueError("trial_read_only")
        if not pending.exists():
            return
        value = self._verify(json.loads(read_private(pending, MAX_EVENT_BYTES + 4096)))
        if set(value) != {"before", "after", "event", "authentication"}:
            raise ValueError("trial_pending_invalid")
        before, after = self._verify(value["before"]), self._verify(value["after"])
        head = self._head(enforce_expected=False)
        if self._expected_head is not None and self._expected_head not in {digest(before), digest(after)}:
            raise ValueError("trial_trusted_latest_head_mismatch")
        raw = read_private(self.directory / "events.jsonl", MAX_LEDGER_BYTES)
        event = canonical(value["event"])
        if (after["sequence"] != before["sequence"] + 1 or after["bytes"] != before["bytes"] + len(event)
                or after["event_sha256"] != digest(value["event"]) or value["event"]["previous"] != before["event_sha256"]
                or any(item["plan_sha256"] != self.plan["plan_sha256"] for item in (before, after, value["event"]))):
            raise ValueError("trial_pending_invalid")
        if head not in (before, after) or len(raw) < before["bytes"] or len(raw) > after["bytes"]:
            raise ValueError("trial_append_conflict")
        tail = raw[before["bytes"]:]
        if not event.startswith(tail):
            raise ValueError("trial_append_conflict")
        if len(raw) != after["bytes"]:
            if head != before:
                raise ValueError("trial_append_conflict")
            descriptor = os.open(self.directory / "events.jsonl", os.O_WRONLY | os.O_NOFOLLOW)
            try:
                os.ftruncate(descriptor, before["bytes"]); os.lseek(descriptor, 0, os.SEEK_END)
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(event); stream.flush(); os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
        replace_private(self.directory / "head.json", after)
        self._expected_head = digest(after)
        pending.unlink()

    @contextlib.contextmanager
    def locked(self):
        import fcntl

        checked_directory(self.directory)
        current = self.directory.stat()
        if [current.st_dev, current.st_ino] != self._directory_identity:
            raise ValueError("trial_directory_changed")
        checked_directory(self.work_root)
        info = self.work_root.stat()
        if [info.st_dev, info.st_ino] != self.binding["work_root_identity"]:
            raise ValueError("trial_work_root_changed")
        flags = os.O_RDONLY if self._read_only else os.O_RDWR | os.O_CREAT
        descriptor = os.open(self.directory / "ledger.lock", flags | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                raise ValueError("trial_lock_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("trial_busy") from None
            identity = [info.st_dev, info.st_ino]
            if self._lock_identity not in (None, identity):
                raise ValueError("trial_lock_changed")
            self._lock_identity = identity
            self._lock_thread = threading.get_ident()
            pending = self.directory / "pending.json"
            if self._read_only and (pending.exists() or pending.is_symlink()):
                raise ValueError("trial_read_only_recovery_required")
            yield
            entry = (self.directory / "ledger.lock").stat(follow_symlinks=False)
            if [entry.st_dev, entry.st_ino] != identity:
                raise ValueError("trial_lock_changed")
        finally:
            self._lock_thread = None
            os.close(descriptor)

    def events(self):
        raw = read_private(self.directory / "events.jsonl", MAX_LEDGER_BYTES)
        head = self._head()
        if len(raw) != head["bytes"]:
            raise ValueError("trial_retention_incomplete")
        if raw and not raw.endswith(b"\n"):
            raise ValueError("trial_retention_incomplete")
        lines = raw.splitlines()
        if len(lines) > MAX_EVENTS:
            raise ValueError("trial_event_limit")
        rows, previous = [], None
        for index, line in enumerate(lines):
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("trial_event_limit")
            row = json.loads(line)
            if (type(row) is not dict or set(row) != {"sequence", "plan_sha256", "previous", "kind", "data", "at", "authentication"}
                    or row["sequence"] != index + 1 or row["previous"] != previous
                    or row["plan_sha256"] != self.plan["plan_sha256"]):
                raise ValueError("trial_event_corrupt")
            signature = hmac.new(self.key, canonical({**row, "authentication": None}), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, row["authentication"]):
                raise ValueError("trial_event_authentication_failed")
            rows.append(row)
            previous = digest(row)
        if len(rows) != head["sequence"] or previous != head["event_sha256"]:
            raise ValueError("trial_retention_incomplete")
        return rows

    def append(self, kind, data):
        if self._read_only:
            raise ValueError("trial_read_only")
        rows = self.events()
        if len(rows) >= MAX_EVENTS:
            raise ValueError("trial_event_limit")
        row = {"sequence": len(rows) + 1, "plan_sha256": self.plan["plan_sha256"],
            "previous": digest(rows[-1]) if rows else None, "kind": kind, "data": data,
            "at": clock(), "authentication": None}
        row["authentication"] = hmac.new(self.key, canonical(row), hashlib.sha256).hexdigest()
        raw = canonical(row)
        path = self.directory / "events.jsonl"
        if len(raw) > MAX_EVENT_BYTES or path.stat().st_size + len(raw) > MAX_LEDGER_BYTES:
            raise ValueError("trial_event_limit")
        before = self._head()
        after = self._signed({"sequence": row["sequence"], "bytes": before["bytes"] + len(raw),
            "event_sha256": digest(row), "plan_sha256": self.plan["plan_sha256"]})
        publish_new(self.directory / "pending.json", self._signed({"before": before, "after": after, "event": row}))
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        replace_private(self.directory / "head.json", after)
        self._expected_head = digest(after)
        (self.directory / "pending.json").unlink()

    def attempts(self):
        attempts = {}
        for event in self.events():
            if event["kind"] == "attempt":
                row = event["data"]
                before = attempts.get(row["attempt_id"])
                if before is not None and any(before[field] != row[field] for field in
                        ("slot_id", "attempt_index", "parent_attempt_id", "plan_sha256", "packet_sha256", "reserved_at")):
                    raise ValueError("trial_attempt_binding_changed")
                if before is not None and before["finished_at"] is not None:
                    raise ValueError("trial_terminal_attempt_rewritten")
                attempts[row["attempt_id"]] = row
        result = list(attempts.values())
        project_report(self.plan, result)
        return result

    def sealed(self):
        seals = [row for row in self.events() if row["kind"] == "generation_sealed"]
        if len(seals) > 1:
            raise ValueError("trial_seal_conflict")
        if seals and seals[0]["data"] != {"attempts_sha256": digest(self.attempts())}:
            raise ValueError("trial_seal_changed")
        return bool(seals)

    def reserve(self, slot_id, *, retry=False):
        with self.locked():
            if self.sealed():
                raise ValueError("trial_generation_closed")
            packet = generator_packet(self.plan, slot_id)
            rows = self.attempts()
            prior = [row for row in rows if row["slot_id"] == slot_id]
            if prior and not retry:
                return prior[-1], packet, False
            if any(row["status"] in {"reserved", "running"} for row in rows):
                raise ValueError("trial_active_attempt_unsettled")
            if retry and (not prior or prior[-1]["status"] not in {"failed", "abandoned", "abstained"}):
                raise ValueError("trial_retry_unsettled_or_not_failed")
            if len(prior) >= self.plan["budgets"]["max_attempts"]:
                raise ValueError("trial_attempt_budget_exhausted")
            first = None if not prior else datetime.fromisoformat(prior[0]["reserved_at"])
            if first is not None and datetime.now(timezone.utc) >= first + timedelta(seconds=self.plan["budgets"]["seconds"]):
                raise ValueError("trial_slot_deadline_exhausted")
            if not prior:
                expected = next((slot for slot in self.plan["schedule"]
                    if not any(row["slot_id"] == slot["slot_id"] for row in rows)), None)
                if expected is None or expected["slot_id"] != slot_id:
                    raise ValueError("trial_assignment_order_changed")
            identifier = slot_id + "." + str(len(prior) + 1)
            at = clock()
            row = {"attempt_id": identifier, "slot_id": slot_id, "attempt_index": len(prior) + 1,
                "parent_attempt_id": prior[-1]["attempt_id"] if prior else None,
                "plan_sha256": self.plan["plan_sha256"], "packet_sha256": digest(packet),
                "reserved_at": at, "started_at": None, "finished_at": None, "status": "reserved", "answer": None,
                "receipt": {"provenance": "host_observed", "kind": "unobserved", "input_sha256": digest(packet),
                    "output_sha256": None, "elapsed_seconds": None, "exit_code": None,
                    "model_calls": None, "tool_calls": None, "tokens": None, "cost": None, "native_receipt_refs": []}}
            # Reserve closure capacity before any dispatch. The executable host
            # supports a narrower study than the pure projection's inventory.
            remaining_events = 3 * self.plan["budgets"]["max_attempts"] * len(self.plan["schedule"]) + 4
            remaining_bytes = 300_000 * self.plan["budgets"]["max_attempts"] * len(self.plan["schedule"]) + 65_536
            if (remaining_events > MAX_EVENTS or remaining_bytes > MAX_LEDGER_BYTES
                    or len(self.events()) + 3 > MAX_EVENTS
                    or self._head()["bytes"] + 300_000 > MAX_LEDGER_BYTES):
                raise ValueError("trial_closure_capacity_unavailable")
            project_report(self.plan, [*rows, row])
            self.append("attempt", row)
            return row, packet, True

    def observe(self, attempt_id, *, status, answer=None, started_at=None, receipt=None):
        with self.locked():
            if self.sealed():
                raise ValueError("trial_generation_closed")
            rows = self.attempts()
            matches = [row for row in rows if row["attempt_id"] == attempt_id]
            if len(matches) != 1 or matches[0]["finished_at"] is not None:
                raise ValueError("trial_attempt_unavailable_or_terminal")
            row = {**matches[0], "status": status, "answer": answer,
                "started_at": started_at or matches[0]["started_at"],
                "finished_at": None if status == "running" else clock()}
            if matches[0]["started_at"] is not None and started_at not in (None, matches[0]["started_at"]):
                raise ValueError("trial_confirmed_start_changed")
            if receipt is not None:
                row["receipt"] = receipt
            project_report(self.plan, [row if item["attempt_id"] == attempt_id else item for item in rows])
            self.append("attempt", row)
            return row

    def seal_generation(self):
        with self.locked():
            if self.sealed():
                return final_judgment_packet(self.plan, self.attempts(), generation_closed=True, binding_outcomes_sealed=True)
            rows = self.attempts()
            packet = final_judgment_packet(self.plan, rows, generation_closed=True, binding_outcomes_sealed=True)
            self.append("generation_sealed", {"attempts_sha256": digest(rows)})
            return packet

    def record_judgments(self, judgments, *, grading_receipt=None):
        with self.locked():
            if not self.sealed():
                raise ValueError("trial_generation_not_sealed")
            seal = next(row for row in self.events() if row["kind"] == "generation_sealed")
            if grading_receipt is not None:
                started = datetime.fromisoformat(grading_receipt["started_at"])
                completed = datetime.fromisoformat(grading_receipt["completed_at"])
                budget = self.plan["judging_budget"]
                if (started < datetime.fromisoformat(seal["at"]) or budget is None
                        or not 0 <= (completed - started).total_seconds() <= budget["seconds"]):
                    raise ValueError("trial_grading_seal_or_lifetime_mismatch")
            supplied = {"judgments": judgments, "grading_receipt": grading_receipt}
            prior = [row for row in self.events() if row["kind"] == "judgments"]
            if prior:
                if len(prior) != 1 or prior[0]["data"] != supplied:
                    raise ValueError("trial_judgment_replay_conflict")
            else:
                project_report(self.plan, self.attempts(), judgments,
                    generation_closed=True, binding_outcomes_sealed=True, grading_receipt=grading_receipt)
                self.append("judgments", supplied)
            return self.report()

    def report(self):
        if self._lock_thread != threading.get_ident():
            with self.locked():
                return self.report()
        # Callers performing a mutation already hold the nonblocking host lock.
        sealed = self.sealed()
        judges = [row["data"] for row in self.events() if row["kind"] == "judgments"]
        if len(judges) > 1:
            raise ValueError("trial_judgment_replay_conflict")
        result = project_report(self.plan, self.attempts(), judges[0]["judgments"] if judges else (),
            generation_closed=sealed, binding_outcomes_sealed=sealed,
            grading_receipt=judges[0]["grading_receipt"] if judges else None)
        result["retention_head_sha256"] = digest(self._head())
        result["grading_invocation_qualification"] = "host_observed; model/profile qualification unknown" if judges and judges[0]["grading_receipt"] else "unverified_import" if judges else "unobserved"
        result["restore_qualification"] = "host-private signed head; full-store rollback requires external latest-head custody"
        return result

    def dispatch(self, slot_id, command, *, retry=False):
        """Run an explicitly selected common command; no implicit model discovery."""
        if (type(command) is not list or not command or any(type(part) is not str or not part for part in command)
                or self.plan["conditions"]["policy"].get("generator_command_sha256") != digest(command)
                or self.plan["conditions"]["policy"].get("generator_files_sha256") != digest(command_manifest(command))):
            raise ValueError("trial_generator_command_changed")
        row, packet, fresh = self.reserve(slot_id, retry=retry)
        if not fresh:
            return row
        started = time.monotonic()
        process = None
        receipt = {**row["receipt"], "kind": "scripted_process"}
        # A command or model label is no proof of live provider execution. Model
        # usage stays unknown unless an independently selected observer supplies it.
        try:
            directory = self.work_root / row["attempt_id"]
            directory.mkdir(mode=0o700)
            publish_new(directory / "input.json", packet)
            first = next(item for item in self.attempts() if item["slot_id"] == slot_id)
            deadline = datetime.fromisoformat(first["reserved_at"]) + timedelta(seconds=self.plan["budgets"]["seconds"])
            with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                stdin.write(canonical(packet)); stdin.seek(0)
                environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                if datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError("trial_deadline_exhausted")
                if self.plan["conditions"]["policy"]["generator_files_sha256"] != digest(command_manifest(command)):
                    raise ValueError("trial_command_file_changed")
                process = subprocess.Popen(command, cwd=directory, env=environment, stdin=stdin, stdout=stdout,  # noqa: S603 -- explicit frozen host argv.
                    stderr=stderr, start_new_session=True)
                self.observe(row["attempt_id"], status="running", started_at=clock(), receipt=receipt)
                while process.poll() is None:
                    if datetime.now(timezone.utc) >= deadline:
                        raise TimeoutError("trial_deadline_exhausted")
                    if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > MAX_EVENT_BYTES:
                        raise ValueError("trial_output_overflow")
                    time.sleep(0.02)
                stop_process(process)
                if datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError("trial_deadline_exhausted")
                if self.plan["conditions"]["policy"]["generator_files_sha256"] != digest(command_manifest(command)):
                    raise ValueError("trial_command_file_changed")
                stdout.seek(0); raw = stdout.read(MAX_EVENT_BYTES + 1)
                if len(raw) + os.fstat(stderr.fileno()).st_size > MAX_EVENT_BYTES:
                    raise ValueError("trial_output_overflow")
                receipt.update(elapsed_seconds=time.monotonic() - started, exit_code=process.returncode,
                    output_sha256=hashlib.sha256(raw).hexdigest())
                if process.returncode != 0:
                    return self.observe(row["attempt_id"], status="failed", receipt=receipt)
                answer = json.loads(raw)
                slot = next(slot for slot in self.plan["schedule"] if slot["slot_id"] == slot_id)
                case = next(case for case in self.plan["cases"] if case["id"] == slot["case_id"])
                sources = case["sources"] if not slot["cycle_index"] else case["cycles"][slot["cycle_index"] - 1]["sources"]
                answer = normalize_answer(answer, original_text=case["original_text"], sources=sources)
                if datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError("trial_deadline_exhausted")
                publish_new(directory / "answer.json", answer)
                if datetime.now(timezone.utc) >= deadline:
                    raise TimeoutError("trial_deadline_exhausted")
                return self.observe(row["attempt_id"], status="succeeded", answer=answer, receipt=receipt)
        except BaseException as error:
            if process is not None:
                stop_process(process)
            receipt.update(elapsed_seconds=time.monotonic() - started,
                exit_code=None if process is None else process.returncode)
            status = "interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"
            current = next(item for item in self.attempts() if item["attempt_id"] == row["attempt_id"])
            if current["finished_at"] is None:
                self.observe(row["attempt_id"], status=status, receipt=receipt)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            return next(item for item in self.attempts() if item["attempt_id"] == row["attempt_id"])
        finally:
            if process is not None:
                stop_process(process)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "seal", "report", "judge"))
    parser.add_argument("--directory", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--observer-key", required=True, help="Explicit owner-private 32–128-byte key file.")
    parser.add_argument("--config", help="Freeze configuration or private judgment array.")
    parser.add_argument("--slot-id")
    parser.add_argument("--command", help="Frozen common executable argv as a JSON array.")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--expected-head", help="Latest host-retained head from a separate trusted custody boundary; required when reopening.")
    args = parser.parse_args(argv)
    key = read_private(Path(args.observer_key), 128)
    if args.action == "freeze":
        if not args.config:
            parser.error("freeze requires --config")
        plan = freeze_plan(**json.loads(read_private(Path(args.config), MAX_EVENT_BYTES)))
    else:
        plan = json.loads(read_private(Path(args.directory) / "plan.json", MAX_EVENT_BYTES))["plan"]
    ledger = TrialLedger(args.directory, plan, observer_key=key, work_root=args.work_root,
        create=args.action == "freeze", expected_head=args.expected_head)
    if args.action == "run":
        if not args.slot_id or not args.command:
            parser.error("run requires --slot-id and --command")
        result = ledger.dispatch(args.slot_id, json.loads(args.command), retry=args.retry)
    elif args.action == "seal":
        result = ledger.seal_generation()
    elif args.action == "judge":
        if not args.config:
            parser.error("judge requires --config")
        result = ledger.record_judgments(json.loads(read_private(Path(args.config), MAX_EVENT_BYTES)))
    else:
        result = ledger.report()
    print(json.dumps({"result": result, "retention_head_sha256": digest(ledger._head())},
        sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
