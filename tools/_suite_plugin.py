"""Record collection, execution order and resource bounds for one pytest process."""

import json
import os
import sys
from pathlib import Path

_collected = []
_executed = []


def progress(event):
    """Retain completed failures and the active case even if pytest is terminated."""
    target = os.environ.get("EVIDENCE_WIKI_SUITE_RECORD")
    if target:
        with Path(target).with_suffix(".progress.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event) + "\n")


def pytest_collection_finish(session):
    _collected[:] = [item.nodeid for item in session.items]


def pytest_runtest_logstart(nodeid, location):
    _executed.append(nodeid)
    progress({"event": "started", "nodeid": nodeid})


def pytest_runtest_logreport(report):
    event = {"event": "report", "nodeid": report.nodeid, "phase": report.when,
             "outcome": report.outcome, "seconds": report.duration}
    if report.failed:
        event["failure"] = report.longreprtext
    progress(event)


def pytest_sessionfinish(session, exitstatus):
    target = os.environ.get("EVIDENCE_WIKI_SUITE_RECORD")
    if not target:
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
              "outcomes": {key: len(rows) for key, rows in terminal.stats.items()}, "memory": memory}
    Path(target).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
