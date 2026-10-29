"""Bounded process scheduling for trusted, current original-owner reads."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import os
import secrets
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from tools._integration_improvement import INTEGRITY_ERRORS, OwnerEvidence, OwnerReader, validate_plan
from tools._release_decision import OriginalCitation, _original, bounded_json, canonical, digest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("tools/_integration_owner_pass.py", "tools/read_integration_owner.py",
                "tools/_integration_improvement.py", "tools/_integration_improvement_owners.py",
                "tools/run_repeated_integrations.py", "tools/replay_qualification_native.py",
                "tools/_native_owner_execution.py", "src/evidence_wiki/_native_owner_execution.py",
                "src/evidence_wiki/fixture_execution.py",
                "tools/_native_child_registry.py", "tools/_command_completion.py",
                "src/evidence_wiki/_process_groups.py",
                "tools/measure_integration_costs.py", "tools/_repeated_integration_host.py",
                "examples/unattended-host/fixture_host.py", "examples/unattended-host/finite_host.py")
MAX_REQUEST_BYTES = 1_048_576
MAX_RAW_BYTES = 1_048_576
MAX_FRAME_BYTES = 2_097_152
MAX_GROUP_BYTES = 3 * MAX_FRAME_BYTES
PASS_SECONDS = 120


@contextmanager
def _defer_signals():
    """Make process ownership and final cleanup atomic against cancellation."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def source_manifest():
    """Pin the fixed controller and worker code independently of wire claims."""
    result = {}
    for name in SOURCE_FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("integration_current_candidate_changed")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _isolated_roots(originals):
    """Validate fixed producer layouts without loading any host's factory."""
    from tools.run_comparative_trials import read_private

    groups = []
    for selected in originals.values():
        path = Path(selected["path"])
        if (not path.is_absolute() or path.name != "report.json" or path.resolve(strict=True) != path
                or path.parent.is_relative_to(ROOT)):
            raise ValueError("integration_owner_binding")
        raw = read_private(path, MAX_RAW_BYTES)
        if hashlib.sha256(raw).hexdigest() != selected["raw_sha256"]:
            raise ValueError("integration_original_head_changed")
        report = _original(raw)
        if type(report) is not dict or digest(report) != selected["source_head_sha256"]:
            raise ValueError("integration_original_head_changed")
        directory = path.parent
        fixture = directory / "fixture"
        workspace, private, control = fixture / "workspace", fixture / "fixture-private", fixture / "fixture-control"
        roots = (directory, fixture, workspace, private, control)
        if (any(root.resolve(strict=False) != root for root in roots)
                or report.get("workspace") not in {None, str(workspace)}
                or report.get("factory") not in {None, str(private / "services.py")}
                or (report.get("failure") is None and
                    (report.get("workspace") != str(workspace) or report.get("factory") != str(private / "services.py")))):
            raise ValueError("integration_original_native_paths")
        for previous in groups:
            if any(left.is_relative_to(right) or right.is_relative_to(left) for left in roots for right in previous):
                raise ValueError("integration_owner_binding")
        groups.append(roots)


def _frame(request, slot_id, *, evidence=None, error=None):
    value = {"request_sha256": digest(request), "host_id": request["host_id"], "slot_id": slot_id}
    if evidence is not None:
        if type(evidence) is not OwnerEvidence or len(evidence.raw) > MAX_RAW_BYTES:
            raise ValueError("integration_owner_type")
        data = asdict(evidence)
        data["raw"] = base64.b64encode(evidence.raw).decode("ascii")
        data["citations"] = list(data["citations"])
        value.update(kind="evidence", data=data)
    else:
        # Error strings, tracebacks, factory contents and authority never cross
        # the worker boundary. Only original integrity codes retain stop scope.
        value.update(kind="error", data={"value_error": isinstance(error, ValueError),
                     "integrity_code": str(error) if isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS else None})
    return canonical(bounded_json(value, MAX_FRAME_BYTES))


def _decode_frame(raw, request, expected_slot):
    value = bounded_json(json.loads(raw), MAX_FRAME_BYTES)
    if (type(value) is not dict or set(value) != {"request_sha256", "host_id", "slot_id", "kind", "data"}
            or value["request_sha256"] != digest(request) or value["host_id"] != request["host_id"]
            or value["slot_id"] != expected_slot):
        raise ValueError("integration_owner_binding")
    data = value["data"]
    if value["kind"] == "error":
        if (type(data) is not dict or set(data) != {"value_error", "integrity_code"}
                or type(data["value_error"]) is not bool
                or (data["integrity_code"] is not None and
                    (data["integrity_code"] not in INTEGRITY_ERRORS or not data["value_error"]))):
            raise ValueError("integration_owner_binding")
        return ValueError(data["integrity_code"] or "integration_owner_invalid") if data["value_error"] else OSError()
    fields = {"raw", "source_head_sha256", "candidate_sha256", "host_sha256", "input_sha256", "observed_at",
              "expires_at", "evidence_class", "projection", "citations"}
    if value["kind"] != "evidence" or type(data) is not dict or set(data) != fields:
        raise ValueError("integration_owner_binding")
    if type(data["raw"]) is not str or type(data["citations"]) is not list or not 1 <= len(data["citations"]) <= 32:
        raise ValueError("integration_owner_binding")
    original = base64.b64decode(data["raw"], validate=True)
    if len(original) > MAX_RAW_BYTES:
        raise ValueError("cost_output_overflow")
    citations = []
    for citation in data["citations"]:
        if type(citation) is not dict or set(citation) != {"pointer", "value"}:
            raise ValueError("integration_owner_binding")
        citations.append(OriginalCitation(**citation))
    return OwnerEvidence(**(data | {"raw": original, "citations": tuple(citations)}))


def _group_alive(process):
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some host sandboxes deny signals to a group after its owned leader
        # has exited. A denied signal is not evidence that the group is gone.
        if sys.platform == "darwin":
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_listpids
            function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
            function.restype = ctypes.c_int
            pids = (ctypes.c_int * 32768)()
            ctypes.set_errno(0)
            # PROC_PGRP_ONLY is the public, read-only process-group selector.
            size = function(2, process.pid, pids, ctypes.sizeof(pids))
            if (ctypes.get_errno() != 0 or size < 0 or size >= ctypes.sizeof(pids)
                    or size % ctypes.sizeof(ctypes.c_int)):
                raise RuntimeError("integration_owner_cleanup_unconfirmed") from None
            return any(pids[index] > 0 for index in range(size // ctypes.sizeof(ctypes.c_int)))
        with tempfile.TemporaryFile() as listing:
            result = subprocess.run(["/bin/ps", "-axo", "pgid="],  # noqa: S603 -- fixed read-only process inventory.
                                    stdout=listing, stderr=subprocess.DEVNULL, timeout=2, check=False)
            if result.returncode != 0 or listing.tell() > MAX_REQUEST_BYTES:
                raise RuntimeError("integration_owner_cleanup_unconfirmed") from None
            listing.seek(0)
            groups = listing.read(MAX_REQUEST_BYTES + 1).split()
        if any(not group.isdigit() for group in groups):
            raise RuntimeError("integration_owner_cleanup_unconfirmed") from None
        return str(process.pid).encode() in groups
    return True


def _stop_group(process):
    """Confirm both leader reaping and disappearance of its process group."""
    from evidence_wiki._process_groups import ownership_for

    guard = ownership_for(process)
    if guard is not None:
        guard.stop()
        return
    if process.poll() is not None and not _group_alive(process):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        if _group_alive(process):
            raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        if _group_alive(process):
            raise
    process.wait(timeout=5)
    limit = time.monotonic() + 5
    while _group_alive(process):
        if time.monotonic() >= limit:
            raise RuntimeError("integration_owner_cleanup_unconfirmed")
        time.sleep(0.01)


@dataclass
class _Session:
    process: subprocess.Popen
    request: dict
    slots: tuple[str, ...]
    pending: bytes = b""
    total: int = 0
    index: int = 0
    cleaned: bool = False


def _launch(request):
    raw = canonical(bounded_json(request, MAX_REQUEST_BYTES))
    # The temporary input has private permissions and cannot block on a full
    # pipe. The child receives data, never a selected command or factory.
    process = None
    try:
        with tempfile.TemporaryFile() as source:
            source.write(raw)
            source.seek(0)
            process = subprocess.Popen(  # noqa: S603 -- fixed trusted worker and interpreter.
                [str(ROOT / ".venv/bin/python"), str(ROOT / "tools/read_integration_owner.py")],
                cwd=ROOT, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        os.set_blocking(process.stdout.fileno(), False)
    except BaseException:
        if process is not None:
            try:
                _stop_group(process)
            finally:
                process.stdout.close()
        raise
    return process


def _collect(requests, *, workers, deadline):
    """Preserve completed frames when another group fails or exhausts time."""
    outcomes, queue, active, spawned, integrity_errors, incomplete = {}, list(requests), {}, [], [], set()
    with selectors.DefaultSelector() as selector:
        def finish(session, error=None):
            if isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS:
                integrity_errors.append(str(error))
            if session.process.stdout in active:
                selector.unregister(session.process.stdout)
                active.pop(session.process.stdout)
            complete = False
            if error is None and session.index == len(session.slots) and not session.pending:
                try:
                    remaining = max(0, deadline - time.monotonic())
                    complete = session.process.wait(timeout=min(.2, remaining)) == 0 and not _group_alive(session.process)
                except subprocess.TimeoutExpired:
                    pass
            if not complete:
                incomplete.add(session.request["host_id"])
            _stop_group(session.process)
            session.cleaned = True
            session.process.stdout.close()
            for sid in session.slots[session.index:]:
                outcomes[sid] = error or OSError()

        try:
            while queue or active:
                while queue and len(active) < workers and time.monotonic() < deadline:
                    request = queue.pop(0)
                    slots = tuple(request["slot_ids"])
                    with _defer_signals():
                        try:
                            process = _launch(request)
                        except (OSError, ValueError):
                            outcomes.update({sid: OSError() for sid in slots})
                            incomplete.add(request["host_id"])
                            continue
                        session = _Session(process, request, slots)
                        spawned.append(session)
                        active[process.stdout] = session
                        selector.register(process.stdout, selectors.EVENT_READ)
                if time.monotonic() >= deadline:
                    break
                if not active:
                    continue
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    session = active[key.fileobj]
                    raw = os.read(key.fileobj.fileno(), 65_536)
                    # A frame arriving after the parent allowance is unknown.
                    if time.monotonic() >= deadline:
                        break
                    if not raw:
                        finish(session, ValueError("integration_original_response_changed") if session.pending else None)
                        continue
                    session.total += len(raw)
                    session.pending += raw
                    if session.total > MAX_GROUP_BYTES:
                        finish(session, ValueError("cost_output_overflow"))
                        continue
                    try:
                        while b"\n" in session.pending:
                            frame, session.pending = session.pending.split(b"\n", 1)
                            if len(frame) + 1 > MAX_FRAME_BYTES:
                                raise ValueError("cost_output_overflow")
                            if session.index >= len(session.slots):
                                raise ValueError("integration_owner_binding")
                            sid = session.slots[session.index]
                            outcomes[sid] = _decode_frame(frame, session.request, sid)
                            session.index += 1
                        if len(session.pending) > MAX_FRAME_BYTES:
                            raise ValueError("cost_output_overflow")
                    except (ValueError, TypeError, KeyError, UnicodeError) as error:
                        # A malformed controlled stream cannot appoint an owner.
                        finish(session, ValueError("cost_output_overflow" if str(error) == "cost_output_overflow"
                                                   else "integration_owner_binding"))
        finally:
            # Cancellation and the outer host deadline also pass through here.
            # Cleanup failure aborts the decision; a new pass cannot start.
            cleanup_error = None
            incomplete.update(session.request["host_id"] for session in active.values())
            incomplete.update(request["host_id"] for request in queue)
            with _defer_signals():
                for session in spawned:
                    try:
                        if not session.cleaned:
                            _stop_group(session.process)
                            session.cleaned = True
                    except (OSError, subprocess.TimeoutExpired, RuntimeError) as error:
                        cleanup_error = error
                    finally:
                        session.process.stdout.close()
            if cleanup_error is not None:
                raise RuntimeError("integration_owner_cleanup_unconfirmed") from None
    for request in requests:
        for sid in request["slot_ids"]:
            outcomes.setdefault(sid, OSError())
    return outcomes, tuple(integrity_errors), tuple(sorted(incomplete))


class NativeOwnerPass:
    """Schedule distinct hosts while retaining all admission in the caller."""

    def __init__(self, plan, originals, *, remaining_seconds):
        from tools._integration_improvement_owners import host_readers
        from tools.verify_acceptance_paths import candidate_manifest

        self.plan = validate_plan(plan, digest(plan))
        plan = self.plan
        self.originals = bounded_json(originals)
        self.workers = plan["scope"].get("owner_read_workers")
        if type(self.workers) is not int or not 1 <= self.workers <= 3:
            raise ValueError("integration_owner_worker_bound")
        if type(remaining_seconds) not in {int, float} or not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise ValueError("integration_owner_pass_duration")
        self.source = source_manifest()
        if (plan["scope"].get("owner_read_controller_sha256") != digest(self.source)
                or digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]):
            raise ValueError("integration_current_candidate_changed")
        self.deadline = time.monotonic() + min(remaining_seconds, 900)
        # Construction checks the host-selected grammar without executing reads.
        originals_readers = host_readers(plan, self.originals)
        if self.workers > 1:
            _isolated_roots(self.originals)
        self.outcomes = {}
        self.failed_cleanup = False
        self.integrity_errors = set()
        self.incomplete_hosts = set()
        self.readers = tuple(OwnerReader(reader.slot_id, reader.owner_ref,
                            lambda sid=reader.slot_id: self._read(sid)) for reader in originals_readers)

    def _read(self, slot_id):
        result = self.outcomes.get(slot_id, OSError())
        if isinstance(result, Exception):
            raise result
        return result

    def prepare_pass(self):
        from tools.verify_acceptance_paths import candidate_manifest

        if self.failed_cleanup:
            raise RuntimeError("integration_owner_cleanup_unconfirmed")
        self.outcomes = {}
        if source_manifest() != self.source or digest(candidate_manifest(ROOT)) != self.plan["candidate_sha256"]:
            self.integrity_errors.add("integration_current_candidate_changed")
            self.outcomes = {reader.slot_id: ValueError("integration_current_candidate_changed") for reader in self.readers}
            return
        if self.workers > 1:
            try:
                _isolated_roots(self.originals)
            except (ValueError, OSError, TypeError) as error:
                self.outcomes = {reader.slot_id: error for reader in self.readers}
                if isinstance(error, ValueError) and str(error) in INTEGRITY_ERRORS:
                    self.integrity_errors.add(str(error))
                else:
                    self.incomplete_hosts.update(self.originals)
                return
        requests = []
        for host in self.plan["hosts"]:
            if host["host_id"] not in self.originals:
                continue
            selected = {host["host_id"]: self.originals[host["host_id"]]}
            requests.append({"schema_version": "integration-owner-request/v1", "pass_nonce": secrets.token_hex(16),
                "plan": self.plan, "plan_sha256": digest(self.plan), "originals": selected,
                "originals_sha256": digest(selected), "source_manifest": self.source, "host_id": host["host_id"],
                "slot_ids": sorted(s["slot_id"] for s in self.plan["slots"] if s["host_id"] == host["host_id"])})
        try:
            self.outcomes, errors, incomplete = _collect(requests, workers=self.workers,
                                                         deadline=min(self.deadline, time.monotonic() + PASS_SECONDS))
            self.integrity_errors.update(errors)
            self.incomplete_hosts.update(incomplete)
        except BaseException:
            self.failed_cleanup = True
            raise
        if source_manifest() != self.source or digest(candidate_manifest(ROOT)) != self.plan["candidate_sha256"]:
            self.integrity_errors.add("integration_current_candidate_changed")
            # Preserve earlier unsafe evidence as well as the closing source
            # failure: the generic closing check independently binds candidate.
            for reader in self.readers:
                if not isinstance(self.outcomes.get(reader.slot_id), OwnerEvidence):
                    self.outcomes[reader.slot_id] = ValueError("integration_current_candidate_changed")
        return frozenset(slot["slot_id"] for slot in self.plan["slots"] if slot["host_id"] in self.incomplete_hosts)

    def closing_check(self):
        """Keep stream integrity failures monotone without replacing originals."""
        if self.failed_cleanup:
            raise RuntimeError("integration_owner_cleanup_unconfirmed")
        if self.integrity_errors:
            raise ValueError(sorted(self.integrity_errors)[0])
        if self.incomplete_hosts:
            raise RuntimeError("integration_owner_worker_unclosed")
