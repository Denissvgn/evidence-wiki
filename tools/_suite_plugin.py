"""Record collection, execution order and resource bounds for one pytest process."""

import json
import os
import signal
import sys
from pathlib import Path

import pytest

MAX_NODE_ID_UNITS = 1024

_collected = []
_executed = []
_case_outcomes = {}
_terminated = False


def progress(event):
    """Retain completed failures and the active case even if pytest is terminated."""
    target = os.environ.get("EVIDENCE_WIKI_SUITE_RECORD")
    if target:
        with Path(target).with_suffix(".progress.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event) + "\n")


def _ignore_signal(signum, frame):
    """Not SIG_IGN: a caught signal resets on exec, so cleanup's own children stay stoppable."""


def pytest_sessionstart(session):
    """Unwind fixtures on a terminate signal; a terminated process claims no record."""
    owner = os.getpid()

    def terminate(signum, frame):
        global _terminated
        if os.getpid() != owner:
            # A fork inherits this handler but must not unwind pytest's copied stack.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)
            return
        signal.signal(signal.SIGTERM, _ignore_signal)
        _terminated = True
        session.shouldstop = "terminated at the process limit"
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)


def pytest_collection_modifyitems(items):
    """Reject oversized IDs before reporting or executing payload-derived cases."""
    _collected[:] = [item.nodeid for item in items]
    _case_outcomes.clear()
    oversized = [(node, len(node.encode("utf-16-le", errors="surrogatepass")) // 2)
                 for node in _collected]
    oversized = [(node, units) for node, units in oversized if units > MAX_NODE_ID_UNITS]
    if oversized:
        examples = "\n".join(f"  {node[:120]!r}: {units} UTF-16 units" for node, units in oversized[:5])
        # Preserve exact IDs in the record, but abort before terminal collection
        # reporting can print the entire rejected payload. No case can execute.
        items.clear()
        raise pytest.UsageError(
            f"{len(oversized)} test IDs exceed {MAX_NODE_ID_UNITS} UTF-16 units. "
            f"Use short explicit parameter IDs; test payloads must stay in parameters.\n{examples}"
        )


def pytest_runtest_logstart(nodeid, location):
    _executed.append(nodeid)
    progress({"event": "started", "nodeid": nodeid})


def pytest_runtest_logreport(report):
    if report.failed:
        _case_outcomes[report.nodeid] = "failed"
    elif report.when == "call" or report.skipped:
        _case_outcomes.setdefault(report.nodeid, report.outcome)
    event = {"event": "report", "nodeid": report.nodeid, "phase": report.when,
             "outcome": report.outcome, "seconds": report.duration}
    if report.failed:
        event["failure"] = report.longreprtext
    progress(event)


def pytest_sessionfinish(session, exitstatus):
    signal.signal(signal.SIGTERM, _ignore_signal)
    target = os.environ.get("EVIDENCE_WIKI_SUITE_RECORD")
    if not target or _terminated:
        return
    memory = {"process_peak_bytes": None, "child_peak_bytes": None,
              "scope": "per-process high-water marks, not aggregate concurrent memory"}
    try:
        import resource
        factor = 1 if sys.platform == "darwin" else 1024
        memory.update(process_peak_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * factor,
                      child_peak_bytes=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * factor)
    except ImportError:
        memory["reason"] = "resource.getrusage unavailable on this platform"
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    record = {"collected": _collected, "executed": _executed, "exit_code": int(exitstatus),
              "case_outcomes": dict(_case_outcomes),
              "outcomes": {key: len(rows) for key, rows in terminal.stats.items()}, "memory": memory}
    Path(target).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
