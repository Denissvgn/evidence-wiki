"""Observable, bounded subprocesses for installed qualification commands."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be finite and positive")
    return seconds


@contextmanager
def interruptible():
    """Let CLI cancellation unwind subprocess cleanup and evidence writers."""
    previous = signal.getsignal(signal.SIGTERM)
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def stop_process(process):
    """Stop the command's process group, including ordinary child processes."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],  # noqa: S603,S607 -- fixed Windows utility.
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)


class CommandRunner:
    """Keep live logs and command outcomes even when a qualification is stopped."""

    def __init__(self, output=None, *, heartbeat=30):
        self.output = Path(output) if output is not None else None
        self.heartbeat = positive_seconds(heartbeat)
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.records = []
        if self.output is not None:
            self.output.mkdir(parents=True, exist_ok=True)

    def event(self, event, label, **values):
        row = {"time": datetime.now(timezone.utc).isoformat(), "event": event, "stage": label, **values}
        with self.lock:
            print("[qualification] " + json.dumps(row, sort_keys=True), file=sys.stderr, flush=True)
            if self.output is not None:
                with (self.output / "progress.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(row, sort_keys=True) + "\n")

    def run(self, argv, *, label, cwd=None, env=None, timeout=900, input=None, stream_stderr=False, expected=(0,)):
        timeout = positive_seconds(timeout)
        if self.cancelled.is_set():
            raise KeyboardInterrupt
        with self.lock:
            number = len(self.records) + 1
            record = {"stage": label, "status": "running", "timeout_seconds": timeout}
            self.records.append(record)
        name = f"{number:03}-" + re.sub(r"[^a-zA-Z0-9_.-]", "-", label)[:100]
        started = time.monotonic()
        self.event("started", label, timeout_seconds=timeout)
        with tempfile.TemporaryDirectory(prefix="qualification-command-") as temporary:
            directory = self.output if self.output is not None else Path(temporary)
            stdout_path, stderr_path = directory / (name + ".stdout.log"), directory / (name + ".stderr.log")
            record.update(stdout=stdout_path.name, stderr=stderr_path.name)
            process = None
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr, tempfile.TemporaryFile() as stdin:
                if input is not None:
                    stdin.write(input.encode("utf-8") if isinstance(input, str) else input)
                    stdin.seek(0)
                environment = dict(os.environ if env is None else env, PYTHONUNBUFFERED="1")
                options = ({"start_new_session": True} if os.name != "nt" else
                           {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
                try:
                    process = subprocess.Popen(argv, cwd=cwd, env=environment, stdin=stdin,  # noqa: S603 -- explicit qualification argv.
                                               stdout=stdout, stderr=stderr, **options)
                    next_heartbeat, offset = self.heartbeat, 0
                    while True:
                        if stream_stderr:
                            with stderr_path.open("rb") as live:
                                live.seek(offset)
                                data = live.read(65_536)
                                offset += len(data)
                            if data:
                                with self.lock:
                                    print(data.decode("utf-8", errors="replace"), end="", file=sys.stderr, flush=True)
                        elapsed = time.monotonic() - started
                        if stdout_path.stat().st_size + stderr_path.stat().st_size > 33_554_432:
                            raise ValueError(f"{label}: command output exceeded 32 MiB; inspect retained logs")
                        if self.cancelled.is_set():
                            raise KeyboardInterrupt
                        if process.poll() is not None:
                            record.update(status="passed" if process.returncode in expected else "failed", exit_code=process.returncode)
                            if stream_stderr:
                                with stderr_path.open("rb") as live:
                                    live.seek(offset)
                                    remaining = live.read().decode("utf-8", errors="replace")
                                if remaining:
                                    with self.lock:
                                        print(remaining, end="", file=sys.stderr, flush=True)
                            break
                        if elapsed >= timeout:
                            record["status"] = "timed_out"
                            raise subprocess.TimeoutExpired(label, timeout)
                        if elapsed >= next_heartbeat:
                            self.event("running", label, seconds=round(elapsed, 2), timeout_seconds=timeout)
                            next_heartbeat = elapsed + self.heartbeat
                        time.sleep(min(0.2, timeout - elapsed, max(0.01, next_heartbeat - elapsed)))
                except BaseException as error:
                    if record["status"] == "running":
                        record["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
                    if process is not None:
                        stop_process(process)
                    raise
                finally:
                    record["seconds"] = round(time.monotonic() - started, 3)
                    self.event(record["status"], label, **{key: value for key, value in record.items() if key not in {"stage", "status"}})
                    if self.output is not None:
                        (directory / (name + ".json")).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(argv, process.returncode,
                stdout_path.read_text(encoding="utf-8", errors="replace"),
                stderr_path.read_text(encoding="utf-8", errors="replace"))
