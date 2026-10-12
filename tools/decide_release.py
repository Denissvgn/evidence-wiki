#!/usr/bin/env python3
"""Freeze, decide and replay release evidence without publishing a release."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import platform
import signal
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools._release_decision import (  # noqa: E402
    _original,
    bounded_json,
    canonical,
    decide,
    digest,
    freeze_plan,
    replay_decision,
    validate_plan,
)
from tools.verify_acceptance_paths import candidate_manifest  # noqa: E402


class _HostDeadline(BaseException):
    """Do not let a reader's ordinary exception recovery consume the deadline."""


class _ProtocolOutput(io.TextIOBase):
    def __init__(self):
        self.attempted = False

    def write(self, text):
        if text:
            self.attempted = True
            raise ValueError("release_host_protocol_output")
        return 0


def _read(path):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError("release_input_not_canonical")
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("release_input_bound")
    return _original(raw)


def _factory_source(selection):
    path_text, separator, name = selection.rpartition(":")
    if not separator or not name.isidentifier():
        raise ValueError("release_factory_selection")
    path = Path(path_text).absolute()
    if path.resolve(strict=True) != path or path.suffix != ".py":
        raise ValueError("release_factory_not_canonical")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o022:
            raise ValueError("release_factory_bound_or_writable")
        raw = stream.read(65_537)
        after = os.fstat(stream.fileno())
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(raw) > 65_536 or identity(before) != identity(after) or identity(path.stat()) != identity(after):
            raise ValueError("release_factory_bound_or_changed")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "entrypoint": name}, raw


def _factory_pin(selection):
    return _factory_source(selection)[0]


def _host_identity(factory):
    """Observe local execution/configuration bytes, not remote host identity."""
    return {"factory": factory, "python": str(Path(sys.executable).resolve()),
            "python_version": platform.python_version(), "system": platform.system(),
            "machine": platform.machine()}


def _publish(path, value):
    path = Path(path).absolute()
    if path.parent.resolve(strict=True) != path.parent:
        raise ValueError("release_output_parent_not_canonical")
    raw = canonical(bounded_json(value))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def _deadline(seconds):
    """Enclose trusted initialization and reads in one POSIX host allowance."""
    if not hasattr(signal, "setitimer"):
        raise ValueError("release_bounded_host_unsupported")
    def expired(_signum, _frame):
        raise _HostDeadline("release_host_deadline")
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError("release_existing_host_timer")
    prior_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)


def _readers(plan, factory):
    pin, raw = _factory_source(factory["path"] + ":" + factory["entrypoint"])
    if pin != factory:
        raise ValueError("release_factory_changed")
    spec = importlib.util.spec_from_file_location("_release_host_selection", factory["path"])
    if spec is None or spec.loader is None:
        raise ValueError("release_factory_unavailable")
    module = importlib.util.module_from_spec(spec)
    output, errors = _ProtocolOutput(), _ProtocolOutput()
    prior_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO("")
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exec(compile(raw, factory["path"], "exec"), module.__dict__)  # noqa: S102 -- explicitly selected, hash-pinned host code
            readers = getattr(module, factory["entrypoint"])(bounded_json(plan))
    finally:
        sys.stdin = prior_stdin
    if output.attempted or errors.attempted:
        raise ValueError("release_factory_protocol_output")
    if _factory_pin(factory["path"] + ":" + factory["entrypoint"]) != factory:
        raise ValueError("release_factory_changed")
    return readers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["freeze", "decide", "replay"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--factory", required=True, help="Trusted host Python file:function; never worker-selected JSON")
    parser.add_argument("--expected-plan")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args(argv)
    try:
        factory = _factory_pin(args.factory)
        host = _host_identity(factory)
        if args.action == "freeze":
            if args.config is None or args.output is None or not 1 <= args.timeout_seconds <= 900:
                raise ValueError("release_freeze_arguments")
            config = _read(args.config)
            if type(config) is not dict or type(config.get("scope")) is not dict:
                raise ValueError("release_config_scope_required")
            if "host_sha256" in config or "runtime" in config.get("scope", {}):
                raise ValueError("release_reserved_host_binding")
            config["host_sha256"] = digest(host)
            config["scope"] = {**config["scope"], "runtime": {"host": host, "timeout_seconds": args.timeout_seconds}}
            plan = freeze_plan(**config)
            _publish(args.output, plan)
            result = {"plan_path": str(args.output), "plan_sha256": digest(plan), "profile": plan["profile"]}
        else:
            if args.plan is None or args.expected_plan is None:
                raise ValueError("release_plan_arguments")
            plan = validate_plan(_read(args.plan), args.expected_plan)
            runtime = plan["scope"].get("runtime")
            if type(runtime) is not dict or runtime.get("host") != host or digest(host) != plan["host_sha256"]:
                raise ValueError("release_current_host_changed")
            if digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]:
                raise ValueError("release_current_candidate_changed")
            remaining = (datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
            if not 1 <= runtime.get("timeout_seconds", 0) <= 900 or remaining <= 0:
                raise ValueError("release_current_window")
            with _deadline(min(remaining, runtime["timeout_seconds"])):
                output, errors = _ProtocolOutput(), _ProtocolOutput()
                prior_stdin = sys.stdin
                try:
                    sys.stdin = io.StringIO("")
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        readers = _readers(plan, factory)
                        if args.action == "replay":
                            if args.report is None:
                                raise ValueError("release_replay_arguments")
                            result = replay_decision(_read(args.report), args.expected_plan, readers)
                        else:
                            if args.output is None:
                                raise ValueError("release_output_required")
                            result = decide(plan, args.expected_plan, readers)
                finally:
                    sys.stdin = prior_stdin
                if output.attempted or errors.attempted:
                    raise ValueError("release_host_protocol_output")
                if _factory_pin(args.factory) != factory or digest(candidate_manifest(ROOT)) != plan["candidate_sha256"]:
                    raise ValueError("release_closing_host_or_candidate_changed")
                if args.action == "decide":
                    _publish(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("outcome", "qualified") == "qualified" else 2
    except (ValueError, OSError, TypeError, TimeoutError, AttributeError, _HostDeadline) as error:
        print(json.dumps({"outcome": "inconclusive", "error": type(error).__name__,
                          "reason": str(error)[:256], "release_published": False}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
