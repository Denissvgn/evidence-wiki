"""The grouped suite gate retains failures and its complete execution inventory."""

import json
import os
import shutil
import signal
import time
from pathlib import Path

import pytest

from tests._script_loader import load_module
from tests.test_coverage_report import REPORT as REPORTER

ROOT = Path(__file__).resolve().parents[1]
GROUPS = load_module("test_groups_tool", ROOT / "tools/run_test_groups.py")


@pytest.mark.parametrize("kind,units,accepted", [
    ("ascii", 1023, True), ("ascii", 1024, True), ("ascii", 1025, False),
    ("unicode", 1024, True), ("unicode", 1025, False),
    ("named", 120000, True), ("automatic", 40000, False), ("multiple", 1025, False),
], ids=["below", "at", "above", "unicode-at", "unicode-above", "named-payload", "automatic-payload", "many"])
def test_collection_id_guard(tmp_path, kind, units, accepted):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n')
    prefix = "tests/test_case.py::test_case["
    identifier_units = units - len(prefix) - 1
    identifier = ("\U0001f642" * (identifier_units // 2) + "x" * (identifier_units % 2)
                  if kind == "unicode" else "x" * identifier_units)
    if kind in {"named", "automatic"}:
        parameter = "@pytest.mark.parametrize('data', ['x' * " + str(units) + "]"
        parameter += ", ids=['large'])" if kind == "named" else ")"
        signature = "data"
    else:
        identifiers = [identifier + str(i) for i in range(8)] if kind == "multiple" else [identifier]
        parameter = "@pytest.mark.parametrize('data', " + repr(list(range(len(identifiers)))) + ", ids=" + repr(identifiers) + ")"
        signature = "data"
    (root / "tests/test_case.py").write_text(
        "import pytest\nfrom pathlib import Path\n" + parameter + "\n"
        + f"def test_case({signature}):\n"
        + "    Path('executed').write_text('yes')\n", encoding="utf-8")
    code = GROUPS.main(["--root", str(root), "--output", str(output)])
    report = json.loads((output / "manifest.json").read_text())
    assert (code == 0) is accepted
    assert (root / "executed").exists() is accepted
    if accepted:
        observed = report["groups"][0]["result"]
        assert observed["collected"] == observed["executed"]
        assert observed["collected"] == report["collection"]["result"]["collected"]
        if kind == "named":
            assert observed["collected"] == [prefix + "large]"]
    else:
        assert report["status"] == "failed" and report["groups"] == []
        assert report["collection"]["result"]["executed"] == []
        log = (output / "collection.log").read_text()
        assert "Use short explicit parameter IDs" in log
        assert "1024 UTF-16 units" in log
        assert len(log) < 2000
        if kind == "multiple":
            assert "8 test IDs" in log and log.count("UTF-16 units") == 6


def test_groups_preserve_order_and_keep_large_modules_intact():
    nodes = ["a.py::a", "a.py::b", "b.py::c", "c.py::d", "c.py::e", "c.py::f"]
    result = GROUPS.groups(nodes, 2)
    assert result == [nodes[:2], nodes[2:3], nodes[3:]]
    assert [node for group in result for node in group] == nodes


def test_source_identity_includes_frozen_data_and_ignores_interpreter_caches(tmp_path):
    fixture = tmp_path / "tests/fixtures/cases.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('{"expected": false}')
    before = GROUPS.source_identity(tmp_path)
    fixture.write_text('{"expected": true}')
    after = GROUPS.source_identity(tmp_path)
    assert before != after and "tests/fixtures/cases.json" in after
    cache = tmp_path / "tests/__pycache__/generated.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"cache")
    assert GROUPS.source_identity(tmp_path) == after


def test_failed_test_remains_failed_with_coverage_and_diagnostics(tmp_path, monkeypatch):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    script = root / "workspace-template/scripts/example.py"
    script.parent.mkdir(parents=True)
    script.write_text("value = 1\n")
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "pyproject.toml").write_text('[tool.coverage.run]\nbranch = true\nparallel = true\n')
    (root / "tests/test_failure.py").write_text(
        'import runpy\nfrom pathlib import Path\n'
        'def test_measured_failure():\n'
        '    runpy.run_path(str(Path(__file__).parents[1] / "workspace-template/scripts/example.py"))\n'
        '    assert False, "intentional behavior failure"\n')
    (root / "tests/test_success.py").write_text('def test_success():\n    assert 1 + 1 == 2\n')
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    snapshot = REPORTER.source_snapshot(root)
    code = GROUPS.main(["--root", str(root), "--output", str(output), "--coverage", "--group-size", "1"])
    assert code == 1
    report = json.loads((output / "manifest.json").read_text())
    assert report["status"] == "failed" and report["sources_unchanged"]
    assert len(report["groups"]) == 2
    assert [row["exit_code"] for row in report["groups"]] == [1, 0]
    assert all(row["selection_matches"] and row["execution_complete"] for row in report["groups"])
    assert "intentional behavior failure" in Path(report["groups"][0]["log"]).read_text()
    assert all(row["result"]["memory"]["scope"] for row in report["groups"])
    coverage = REPORTER.generate_reports(root, root / ".coverage", tmp_path / "coverage", snapshot)
    assert coverage["status"] == "reported"
    assert code == 1 and report["status"] == "failed"


def test_timeout_retains_the_active_case_and_prior_failure(tmp_path):
    root, output = tmp_path / "repo", tmp_path / "evidence"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    output.mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "tests/test_interrupted.py").write_text(
        'import time\n'
        'def test_failure():\n'
        '    assert False, "retained failure before timeout"\n'
        'def test_waiting():\n'
        '    time.sleep(60)\n')
    result = GROUPS.run(root, output, "interrupted", ["tests"], timeout=10)
    assert result["exit_code"] == 124 and "missing_record" in result
    rows = [json.loads(line) for line in (output / "interrupted.progress.jsonl").read_text().splitlines()]
    failures = [row for row in rows if row.get("outcome") == "failed"]
    assert "retained failure before timeout" in failures[0]["failure"]
    assert [row for row in rows if row["event"] == "started"][-1]["nodeid"].endswith("::test_waiting")
    assert not any(row["phase"] == "call" for row in rows
                   if row["event"] == "report" and row["nodeid"].endswith("::test_waiting"))


def process_row(ppid, pgid, start="s", state="S"):
    return {"state": state, "ppid": ppid, "pgid": pgid, "start": start, "command": "python"}


def test_live_tree_follows_links_but_not_recycled_or_exited_processes():
    tracked = {10: process_row(1, 1)}
    table = {
        10: process_row(1, 1),
        11: process_row(10, 11),  # Detached child, still linked by its parent.
        12: process_row(11, 12),  # Nested session under the detached child.
        13: process_row(1, 11),  # Orphan that keeps the group of a tracked leader.
        14: process_row(11, 14, state="Z"),  # Exited but unreaped: holds no resources.
        20: process_row(1, 20),  # Unrelated.
        21: process_row(20, 20),  # Unrelated member of an unrelated group.
        22: process_row(1, 1),  # Shares only the group of pytest's parent.
    }
    assert set(GROUPS.live_tree(table, tracked)) == {10, 11, 12, 13}
    # The leader exits and its group ID stays reserved by the orphan.
    del table[11], table[12]
    assert set(GROUPS.live_tree(table, tracked)) == {10, 13}
    # A recycled PID is a new process, as are its children and group.
    recycled = {10: process_row(1, 1), 11: process_row(1, 11, start="later"), 30: process_row(11, 11)}
    assert set(GROUPS.live_tree(recycled, tracked)) == {10}


@pytest.mark.skipif(os.name != "posix", reason="The process table is read on POSIX only.")
def test_process_table_reports_this_process_stably():
    first, second = GROUPS.process_table(), GROUPS.process_table()
    row = first[os.getpid()]
    assert (row["ppid"], row["pgid"]) == (os.getppid(), os.getpgid(0))
    assert row["start"] and row["command"]
    assert second[os.getpid()]["start"] == row["start"]
    assert not row["state"].startswith("Z")


HOLDER_SOURCE = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOLDER = ("import os,subprocess,sys,time\\n"
          "os.write(os.open(sys.argv[1],os.O_WRONLY),(str(os.getpid())+chr(10)).encode())\\n"
          "if sys.argv[2:]:\\n"
          "    subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]],start_new_session=True)\\n"
          "time.sleep(300)\\n")


def start_detached_tree():
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT / "holders"), HOLDER],
                            start_new_session=True, stdin=subprocess.DEVNULL)


def wait_for_limit():
    time.sleep(300)
"""


def timed_out_repo(tmp_path, body):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "tests/test_held.py").write_text(HOLDER_SOURCE + body, encoding="utf-8")
    os.mkfifo(root / "holders")
    return root, os.open(root / "holders", os.O_RDONLY | os.O_NONBLOCK)


def holders_after_run(reader):
    """End-of-file needs every write end closed, which only process exit does for a holder."""
    data, closed = b"", False
    deadline = time.monotonic() + 10
    while not closed and time.monotonic() < deadline:
        try:
            chunk = os.read(reader, 4096)
        except BlockingIOError:
            time.sleep(0.05)
            continue
        data += chunk
        closed = not chunk
    os.close(reader)
    pids = [int(value) for value in data.split()]
    if not closed:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return pids, closed


@pytest.mark.skipif(os.name != "posix", reason="Detached sessions and named pipes require POSIX.")
def test_timeout_unwinds_fixtures_and_removes_orphaned_detached_descendants(tmp_path):
    """Cleanup stops its direct child only, so a nested session outlives pytest as an orphan."""
    root, reader = timed_out_repo(tmp_path, """
def test_held():
    child = start_detached_tree()
    try:
        wait_for_limit()
    finally:
        os.killpg(child.pid, signal.SIGTERM)
        (ROOT / "cleanup-ran").write_text("yes")
""")
    (root / "tests/test_later.py").write_text("def test_later():\n    pass\n", encoding="utf-8")
    output = tmp_path / "evidence"
    code = GROUPS.main(["--root", str(root), "--output", str(output), "--group-size", "1", "--timeout", "10"])
    pids, closed = holders_after_run(reader)
    assert len(pids) == 2
    assert closed, "a detached descendant outlived the timed-out group"
    assert (root / "cleanup-ran").read_text() == "yes"
    report = json.loads((output / "manifest.json").read_text())
    assert code == 1 and report["status"] == "failed"
    row, later = report["groups"]
    assert later["exit_code"] == 0 and later["execution_complete"] and "timeout_cleanup" not in later
    assert set(row) == {"label", "command", "exit_code", "seconds", "log", "record", "missing_record",
                        "selection_matches", "execution_complete", "timeout_cleanup"}
    assert row["exit_code"] == 124
    assert not row["selection_matches"] and not row["execution_complete"]
    assert not Path(row["record"]).exists()
    cleanup = row["timeout_cleanup"]
    assert cleanup["exited_in_grace"] and cleanup["survivors"] == []
    assert [entry["pid"] for entry in cleanup["forced"]] == [pids[1]]


@pytest.mark.skipif(os.name != "posix", reason="Detached sessions and named pipes require POSIX.")
def test_timeout_kills_the_detached_tree_when_pytest_refuses_to_unwind(tmp_path):
    root, reader = timed_out_repo(tmp_path, """
def test_held():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    start_detached_tree()
    wait_for_limit()
""")
    output = tmp_path / "evidence"
    output.mkdir()
    row = GROUPS.run(root, output, "refused", ["tests"], timeout=10, grace=1)
    pids, closed = holders_after_run(reader)
    assert len(pids) == 2
    assert closed, "a detached descendant outlived the forced stop"
    assert row["exit_code"] == 124 and "missing_record" in row
    cleanup = row["timeout_cleanup"]
    assert not cleanup["exited_in_grace"] and cleanup["survivors"] == []
    assert set(pids) < {entry["pid"] for entry in cleanup["forced"]}
    assert row["seconds"] < 10 + 1 + 2 * GROUPS.KILL_CONFIRMATION


@pytest.mark.skipif(os.name != "posix", reason="Forked children require POSIX.")
def test_forked_child_keeps_default_termination(tmp_path):
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "tests").mkdir()
    output = tmp_path / "evidence"
    output.mkdir()
    shutil.copyfile(ROOT / "tools/_suite_plugin.py", root / "tools/_suite_plugin.py")
    (root / "tests/test_forked.py").write_text(
        "import os, signal, time\n"
        "def test_forked():\n"
        "    reader, writer = os.pipe()\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        os.write(writer, b'ready')\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "    os.read(reader, 5)\n"
        "    os.kill(pid, signal.SIGTERM)\n"
        "    status = os.waitpid(pid, 0)[1]\n"
        "    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGTERM\n", encoding="utf-8")
    row = GROUPS.run(root, output, "forked", ["tests"], timeout=60)
    assert row["exit_code"] == 0, Path(row["log"]).read_text()
    assert row["result"]["executed"] == ["tests/test_forked.py::test_forked"]
