"""Optional, version-pinned Pi RPC transport; never an evidence acceptance owner."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .frameworks import refuse

PI_VERSION = "0.87.0"
MAX_FRAME = 1_048_576
MAX_STREAM = 16_777_216
MAX_EVENTS = 4096


class JsonLines:
    """Decode LF records only; Unicode separators remain ordinary JSON content."""

    def __init__(self):
        self.buffer = bytearray()
        self.total = 0
        self.frames = 0

    def feed(self, chunk: bytes) -> list[dict]:
        self.total += len(chunk)
        if self.total > MAX_STREAM:
            refuse("pi_stream_bound")
        self.buffer.extend(chunk)
        result = []
        while b"\n" in self.buffer:
            end = self.buffer.index(b"\n")
            if end > MAX_FRAME:
                refuse("pi_frame_bound")
            raw = bytes(self.buffer[:end]).removesuffix(b"\r")
            del self.buffer[:end + 1]
            self.frames += 1
            if self.frames > MAX_EVENTS:
                refuse("pi_event_bound")
            try:
                from .frameworks import _unique

                item = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                                  parse_constant=lambda _: refuse("pi_nonfinite_json"))
            except (ValueError, UnicodeError, RecursionError):
                refuse("pi_frame_invalid")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                refuse("pi_frame_shape")
            result.append(item)
        if len(self.buffer) > MAX_FRAME:
            refuse("pi_frame_bound")
        return result

    def eof(self):
        if self.buffer:
            refuse("pi_partial_frame")


class PiRpcBridge:
    """One host-selected ephemeral process and cwd, with no automatic replay.

    Only POSIX pipe/process-group behavior is implemented. External host
    authority is required but is not authenticated by a string or by Pi. This
    transport is not a managed runner or a protected strict-delivery host.
    """

    def __init__(self, executable: str | Path, *, cwd: str | Path, state_dir: str | Path,
                 authority: str, provider: str, model: str, environment: dict[str, str] | None = None,
                 extension: str | Path | None = None, assurance: str = "artifact_checked"):
        if os.name != "posix":
            refuse("pi_bridge_platform_unsupported")
        if assurance != "artifact_checked":
            refuse("protected_pi_bridge_unqualified")
        if not isinstance(authority, str) or not authority.strip() or len(authority) > 512:
            refuse("pi_host_authority_required")
        for value in (provider, model):
            if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
                refuse("pi_model_selection_required")
        self.cwd = Path(cwd).expanduser().resolve(strict=True)
        self.state_dir = Path(state_dir).expanduser().resolve(strict=True)
        selected = Path(executable).expanduser().absolute()
        if (not self.cwd.is_dir() or not self.state_dir.is_dir() or not selected.is_file()
                or self.state_dir == self.cwd or self.cwd in self.state_dir.parents):
            refuse("pi_host_roots_invalid")
        self.cwd_identity = self._cwd_identity()
        self.environment = dict(os.environ if environment is None else environment)
        self.environment.update(PI_CODING_AGENT_DIR=str(self.state_dir), PI_OFFLINE="1", PI_TELEMETRY="0")
        from .orchestration import _execute_bounded

        version = _execute_bounded([str(selected), "--version"], cwd=self.cwd, stdin_text="", timeout_seconds=10,
                                   capture_limit=4096, environment=self.environment, inherit_environment=False)
        if version.returncode or version.timed_out or version.stdout_truncated or version.stderr_truncated or version.stdout.strip() != PI_VERSION:
            refuse("pi_version_unqualified")
        argv = [str(selected), "--mode", "rpc", "--offline", "--no-approve", "--no-session", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                "--provider", provider, "--model", model]
        if extension is not None:
            path = Path(extension).expanduser().resolve(strict=True)
            if not path.is_file():
                refuse("pi_extension_unavailable")
            argv.extend(["--extension", str(path)])
        self.process = subprocess.Popen(argv, cwd=self.cwd, env=self.environment, stdin=subprocess.PIPE,  # noqa: S603 -- explicitly selected host executable and fixed arguments.
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.selector = selectors.DefaultSelector()
        for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            os.set_blocking(stream.fileno(), False)
            self.selector.register(stream, selectors.EVENT_READ, name)
        self.decoder = JsonLines()
        self.events = collections.deque()
        self.responses = {}
        self.pending = {}
        self.seen = set()
        self.sequence = 0
        self.stderr_bytes = 0
        self.closed = False
        self.lock = threading.Lock()
        self.session = None
        try:
            self.session = self._state(time.monotonic() + 10)
            if any(event["type"] == "extension_error" for event in self.events):
                refuse("pi_extension_startup_failed")
            if self.session["streaming"] or self.session["queued"] or self.session["model"] != (provider, model):
                refuse("pi_startup_not_idle")
        except BaseException:
            self.close()
            raise

    def _cwd_identity(self):
        stat = self.cwd.stat()
        return stat.st_dev, stat.st_ino

    def _send(self, kind: str, **fields) -> str:
        if self.closed or len(self.pending) >= 8:
            refuse("pi_transport_unavailable")
        self.sequence += 1
        request = f"ew-{self.sequence}-{uuid.uuid4().hex}"
        payload = (json.dumps({"id": request, "type": kind, **fields}, ensure_ascii=False, allow_nan=False) + "\n").encode()
        if len(payload) > MAX_FRAME:
            refuse("pi_request_bound")
        self.pending[request] = kind
        try:
            # Requests are bounded below the pipe buffer for control operations;
            # prompts use the same deadline-aware nonblocking writer.
            descriptor = self.process.stdin.fileno()
            os.set_blocking(descriptor, False)
            end = time.monotonic() + 5
            offset = 0
            while offset < len(payload):
                try:
                    offset += os.write(descriptor, payload[offset:])
                except BlockingIOError:
                    if time.monotonic() > end:
                        refuse("pi_stdin_timeout")
                    self._pump(min(end, time.monotonic() + .05))
        except (OSError, ValueError):
            refuse("pi_stdin_closed")
        return request

    def _pump(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            refuse("pi_timeout")
        for key, _ in self.selector.select(min(remaining, .1)):
            try:
                chunk = os.read(key.fileobj.fileno(), 65_536)
            except BlockingIOError:
                continue
            if not chunk:
                self.selector.unregister(key.fileobj)
                if key.data == "stdout":
                    self.decoder.eof()
                    refuse("pi_eof")
                continue
            if key.data == "stderr":
                self.stderr_bytes += len(chunk)
                if self.stderr_bytes > MAX_STREAM:
                    refuse("pi_stderr_bound")
                continue
            for event in self.decoder.feed(chunk):
                if event["type"] == "response":
                    request = event.get("id")
                    if (not isinstance(request, str) or request not in self.pending or request in self.responses
                            or event.get("command") != self.pending[request] or type(event.get("success")) is not bool):
                        refuse("pi_response_correlation")
                    self.responses[request] = event
                else:
                    if len(self.events) >= MAX_EVENTS:
                        refuse("pi_event_queue_bound")
                    self.events.append(event)

    def _response(self, request: str, deadline: float) -> dict:
        while request not in self.responses:
            self._pump(deadline)
        result = self.responses.pop(request)
        del self.pending[request]
        return result

    def _state(self, deadline: float) -> dict:
        event = self._response(self._send("get_state"), deadline)
        data = event.get("data")
        if not event["success"] or not isinstance(data, dict) or not isinstance(data.get("sessionId"), str):
            refuse("pi_state_invalid")
        model = data.get("model") or {}
        if (type(data.get("isStreaming")) is not bool or type(data.get("isCompacting")) is not bool
                or type(data.get("pendingMessageCount")) is not int):
            refuse("pi_state_invalid")
        state = {"session": data["sessionId"], "model": (model.get("provider"), model.get("id")),
                 "streaming": data["isStreaming"] or data["isCompacting"], "queued": data["pendingMessageCount"]}
        if self.session and (state["session"] != self.session["session"] or state["model"] != self.session["model"]
                             or self._cwd_identity() != self.cwd_identity):
            refuse("pi_session_or_cwd_replaced")
        return state

    def prompt(self, message: str, *, request_id: str, timeout: float = 60,
               cancel: threading.Event | None = None) -> dict:
        try:
            valid = (isinstance(message, str) and 1 <= len(message.encode("utf-8")) <= 65_536
                     and isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request_id) is not None
                     and type(timeout) in {int, float} and math.isfinite(timeout) and 0 < timeout <= 600)
        except (ValueError, UnicodeError, TypeError):
            valid = False
        if not valid:
            refuse("pi_prompt_invalid")
        if not self.lock.acquire(blocking=False):
            refuse("pi_concurrent_prompt")
        accepted = None
        counts = collections.Counter()
        status, reason = "indeterminate", "pi_interrupted"
        try:
            if request_id in self.seen or len(self.seen) >= 256:
                refuse("pi_prompt_replay_refused")
            self.seen.add(request_id)
            end = time.monotonic() + timeout
            before = self._state(end)
            if before["streaming"] or before["queued"] or self.events:
                refuse("pi_session_not_idle")
            sent = self._send("prompt", message=message)
            last_stop = None
            failed = False
            settled = False
            while not (settled and sent in self.responses):
                if cancel is not None and cancel.is_set():
                    if sent in self.responses:
                        accepted = self.responses[sent]["success"]
                    self._cancel()
                    status, reason = "cancelled", "pi_cancelled_no_replay"
                    break
                self._pump(end)
                while self.events:
                    event = self.events.popleft()
                    kind = event["type"]
                    if not re.fullmatch(r"[a-z_]{1,64}", kind):
                        refuse("pi_event_type_invalid")
                    counts[kind] += 1
                    if len(counts) > 64:
                        refuse("pi_event_type_bound")
                    if kind == "message_end":
                        last_stop = event.get("message", {}).get("stopReason", last_stop)
                    if kind == "extension_error" or kind == "tool_execution_end" and event.get("isError"):
                        failed = True
                    if kind == "agent_settled":
                        settled = True
                if sent in self.responses:
                    accepted = self.responses[sent]["success"]
                    if not accepted:
                        self._response(sent, end)
                        status, reason = "rejected", "pi_prompt_rejected"
                        break
            else:
                accepted = self._response(sent, end)["success"]
                state = self._state(end)
                if state["streaming"] or state["queued"]:
                    refuse("pi_settled_state_conflict")
                failed |= last_stop in {"error", "aborted", "length"}
                status, reason = ("failed", "pi_async_failure") if failed else ("settled", "pi_execution_settled")
        except Exception as error:
            reason = getattr(error, "details", {}).get("field", "pi_transport_failure")
            try:
                self.close()
            except OSError:
                reason = "pi_cleanup_unconfirmed"
        finally:
            self.lock.release()
        return {"schema_version": "evidence-pi-transport/v1", "request_id": request_id, "pi_version": PI_VERSION,
                "status": status, "reason": reason, "prompt_accepted": accepted,
                "events": dict(counts), "evidence_acceptance": "not_evaluated", "host_enforced": False,
                "session_binding": hashlib.sha256(str((self.session, self.cwd_identity)).encode()).hexdigest(), "automatic_replay": False}

    def _cancel(self):
        try:
            end = time.monotonic() + 2
            clear = self._send("clear_queue")
            abort = self._send("abort")
            for request in (clear, abort):
                if not self._response(request, end)["success"]:
                    refuse("pi_cancel_incomplete")
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        from .orchestration import _terminate_process_group

        try:
            self.process.poll()  # Reap an exited leader before checking its group.
            _terminate_process_group(self.process, force=False)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                self.process.poll()
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(.02)
            _terminate_process_group(self.process, force=True)
            self.process.wait(timeout=5)
        finally:
            self.selector.close()
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            self.events.clear()
            self.responses.clear()
            self.pending.clear()
            self.decoder.buffer.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
