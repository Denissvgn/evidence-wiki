#!/usr/bin/env python3
"""Host-owned finite deadlines and cooperative cancellation for native work."""

from __future__ import annotations

import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone

from _script_errors import ScriptRefusal


class OperationControl:
    """Keep the original allowance across checkpoints and bounded child work.

    The callback belongs to the trusted host and is never sent to a worker.
    A cancellation refusal establishes stopped local work, not rollback of
    earlier publications or cancellation of unrelated external operations.
    """

    def __init__(self, *, deadline_utc: str, timeout_seconds: float,
                 cancel_requested: Callable[[], bool] | None = None):
        if (type(deadline_utc) is not str or re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", deadline_utc) is None
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 86400
                or cancel_requested is not None and not callable(cancel_requested)):
            self._refuse("OPERATION_CONTROL_INVALID", "configuration")
        try:
            self._deadline = datetime.fromisoformat(deadline_utc.replace("Z", "+00:00"))
        except ValueError:
            self._refuse("OPERATION_CONTROL_INVALID", "deadline")
        self.deadline_utc = deadline_utc
        self.timeout_seconds = float(timeout_seconds)
        self._cancel_requested = cancel_requested
        self._last_utc = datetime.now(timezone.utc)
        self._end = time.monotonic() + min(self.timeout_seconds,
            max(0.0, (self._deadline - self._last_utc).total_seconds()))
        self._probe_requests = queue.Queue(maxsize=1)
        self._probe_results = queue.Queue(maxsize=1)
        self._probe_lock = threading.Lock()
        self._probe_started = False
        self._cancel_unknown = False
        self._cancelled = False
        self._closed = threading.Event()
        self._probe_thread = None

    @staticmethod
    def _refuse(code: str, checkpoint: str) -> None:
        raise ScriptRefusal(code, "Native operation stopped at its control boundary.",
                            exit_code=2, recoverable=False, details={"checkpoint": checkpoint})

    def checkpoint(self, name: str) -> None:
        if self._closed.is_set():
            self._refuse("OPERATION_CONTROL_CLOSED", name)
        now = datetime.now(timezone.utc)
        if now < self._last_utc:
            self._refuse("OPERATION_CLOCK_REGRESSION", name)
        self._last_utc = now
        if now >= self._deadline or time.monotonic() >= self._end:
            self._refuse("OPERATION_DEADLINE_EXCEEDED", name)
        if self._cancel_requested is not None:
            self._observe_cancellation(name)
        now = datetime.now(timezone.utc)
        if now < self._last_utc:
            self._refuse("OPERATION_CLOCK_REGRESSION", name)
        self._last_utc = now
        if now >= self._deadline or time.monotonic() >= self._end:
            self._refuse("OPERATION_DEADLINE_EXCEEDED", name)

    def _probe_cancellation(self) -> None:
        """Reuse one host-only daemon; a blocked probe never starts another."""
        while True:
            if self._closed.is_set():
                return
            remaining = self._end - time.monotonic()
            if remaining <= 0:
                return
            try:
                self._probe_requests.get(timeout=remaining)
            except queue.Empty:
                return
            if self._closed.is_set():
                return
            try:
                observed = self._cancel_requested()
            except BaseException:
                observed = None
            if self._closed.is_set():
                return
            self._probe_results.put(observed)
            if type(observed) is not bool or observed:
                return

    def _observe_cancellation(self, name: str) -> None:
        if self._cancel_unknown:
            self._refuse("OPERATION_CANCEL_UNKNOWN", name)
        if self._cancelled:
            self._refuse("OPERATION_CANCELLED", name)
        allowance = min(0.05, max(0.0, self._end - time.monotonic()))
        started = time.monotonic()
        if not self._probe_lock.acquire(timeout=allowance):
            self._cancel_unknown = True
            self._refuse("OPERATION_CANCEL_UNKNOWN", name)
        try:
            if not self._probe_started:
                self._probe_started = True
                self._probe_thread = threading.Thread(target=self._probe_cancellation, daemon=True,
                    name="evidence-operation-cancellation")
                self._probe_thread.start()
            try:
                self._probe_requests.put_nowait(None)
                observed = self._probe_results.get(timeout=max(0.0, allowance - (time.monotonic() - started)))
            except (queue.Empty, queue.Full):
                self._cancel_unknown = True
                self._refuse("OPERATION_CANCEL_UNKNOWN", name)
            if self._cancel_unknown:
                self._refuse("OPERATION_CANCEL_UNKNOWN", name)
            if type(observed) is not bool:
                self._cancel_unknown = True
                self._refuse("OPERATION_CANCEL_UNKNOWN", name)
            if observed:
                self._cancelled = True
                self._refuse("OPERATION_CANCELLED", name)
        finally:
            self._probe_lock.release()

    def remaining_seconds(self, name: str = "remaining") -> float:
        self.checkpoint(name)
        return max(0.0, self._end - time.monotonic())

    def close(self) -> None:
        """Release the idle host probe; arbitrary blocked callback code stays uncertain."""
        self._closed.set()
        try:
            self._probe_requests.put_nowait(None)
        except queue.Full:
            pass
        thread = self._probe_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.05)

    def snapshot(self) -> dict[str, str | float]:
        """Describe the fixed boundary without granting a fresh allowance."""
        return {"deadline_utc": self.deadline_utc,
                "remaining_seconds": self.remaining_seconds("snapshot")}

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def run(self, command: Sequence[str]) -> int:
        """Wait for a local isolated worker and stop its process group on exit.

        Cleanup has a separate finite one-second allowance after the original
        work deadline; it never authorizes another work attempt.
        """
        if os.name != "posix" or not callable(getattr(os, "killpg", None)):
            self._refuse("OPERATION_PROCESS_BOUNDARY_UNSUPPORTED", "worker_start")
        self.checkpoint("worker_start")
        process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL,  # noqa: S603 - explicit trusted host command, shell=False
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"})
        try:
            while process.poll() is None:
                self.checkpoint("worker_wait")
                try:
                    process.wait(timeout=min(0.02, self.remaining_seconds("worker_wait")))
                except subprocess.TimeoutExpired:
                    pass
            self.checkpoint("worker_complete")
            return process.returncode
        finally:
            self._signal_group(process, signal.SIGTERM)
            if process.poll() is None:
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    self._signal_group(process, signal.SIGKILL)
                    process.wait(timeout=0.8)
            # A finished leader can still have surviving descendants.
            self._signal_group(process, signal.SIGKILL)


def checkpoint(control: OperationControl | None, name: str) -> None:
    if control is not None:
        if not isinstance(control, OperationControl):
            OperationControl._refuse("OPERATION_CONTROL_INVALID", "type")
        control.checkpoint(name)
