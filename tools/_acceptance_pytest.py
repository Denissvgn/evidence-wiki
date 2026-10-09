"""Pytest's actual node and phase observations for a single frozen worker."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import pytest  # noqa: E402

from tools._acceptance_protocol import MAX_FILES, MAX_RECORD_BYTES, bounded_json, canonical_json, digest  # noqa: E402

_PRIVATE = ("credential", "password", "secret", "token", "authentication", "private_key", "signing_key")
_PUBLIC = {"schema_version", "kind", "status", "state", "outcome", "reason", "code", "error",
           "basis_id", "expected_basis_id", "record_ref", "event_ref", "authorized", "accepted",
           "evidence_accepted", "historical", "request_id", "attempt_id", "operation", "action"}


def _public(value, *, narrow=False):
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if any(word in key.lower() for word in _PRIVATE):
                continue
            retained = _public(item, narrow=narrow)
            if not narrow or key in _PUBLIC or key.endswith("sha256") or (
                    type(item) in {dict, list} and retained):
                result[key] = retained
        return result
    if type(value) is list:
        return [_public(item, narrow=narrow) for item in value]
    return value


class AcceptanceRecords:
    """Capture owner returns only after the selected test actually calls them."""

    def __init__(self, request):
        self.request = request
        self.collected = []
        self.collection_errors = []
        self.active = None
        self.items = {}
        self.assertion_lines = {}
        self.observed = {}
        self.temporary_roots = {}
        self.native_before = {}

    def append(self, event):
        path = Path(self.request["events"])
        raw = canonical_json(bounded_json(event, 65536))
        if path.exists() and path.stat().st_size + len(raw) > MAX_RECORD_BYTES:
            raise ValueError("acceptance_event_stream_bound")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())

    def record_owner_result(self, nodeid, owner, result):
        if (nodeid != self.active or type(owner) is not str or not 1 <= len(owner.encode()) <= 256
                or type(result) is not dict):
            raise ValueError("acceptance_owner_result_binding")
        original = bounded_json(result, 32768)
        result = _public(original)
        self.append({"event": "owner_result", "nodeid": nodeid, "owner": owner,
                     "result": result, "result_sha256": digest(result), "owner_result_sha256": digest(original)})

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(str(report.longrepr)[:4096])

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        if len(self.collected) > 512 or any(len(node.encode()) > 1024 for node in self.collected):
            raise pytest.UsageError("acceptance_collection_bound")
        self.items = {item.nodeid: item for item in session.items}
        if self.request["mode"] == "execute" and self.collected != self.request["exact_nodeids"]:
            session.items.clear()
            raise pytest.UsageError("acceptance_collection_drift")

    def pytest_runtest_logstart(self, nodeid, location):
        self.active = nodeid
        self.append({"event": "started", "nodeid": nodeid})

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        report.acceptance_assertion_failure = bool(call.excinfo and call.excinfo.errisinstance(AssertionError))
        if call.when == "setup" and item.funcargs:
            roots = []
            if item.funcargs.get("tmp_path") is not None:
                roots.append(Path(item.funcargs["tmp_path"]))
            # These fixture names are pinned inputs; inspect only their known roots.
            for name in ("accepted_native", "accepted_session"):
                value = item.funcargs.get(name)
                for candidate in value if type(value) is tuple else (value,):
                    root = getattr(candidate, "root", None)
                    if isinstance(root, (str, Path)):
                        roots.append(Path(root))
            base = Path(self.request["basetemp"])
            self.temporary_roots[item.nodeid] = sorted({root.resolve() for root in roots
                if root.resolve().is_relative_to(base) and not root.is_symlink()})
            self.native_before[item.nodeid] = self.native_hashes(item.nodeid)

    def native_hashes(self, nodeid):
        rows = []
        for root in self.temporary_roots.get(nodeid, []):
            for index, path in enumerate(root.rglob("*")):
                if index >= MAX_FILES or len(rows) >= 32:
                    break
                if path.is_symlink() or not path.is_file() or any(word in str(path.relative_to(root)).lower() for word in _PRIVATE):
                    continue
                if path.suffix != ".jsonl" and not path.name.endswith(".state.json"):
                    continue
                if path.stat().st_size <= MAX_RECORD_BYTES:
                    rows.append([str(path.relative_to(Path(self.request["basetemp"]))), hashlib.sha256(path.read_bytes()).hexdigest()])
        return dict(sorted(rows))

    def pytest_runtest_logreport(self, report):
        row = {"event": "report", "nodeid": report.nodeid, "phase": report.when,
               "outcome": report.outcome, "wasxfail": getattr(report, "wasxfail", None),
               "assertion_failure": getattr(report, "acceptance_assertion_failure", False)}
        if report.failed:
            row["failure_sha256"] = hashlib.sha256(report.longreprtext.encode()).hexdigest()
        self.append(row)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_pyfunc_call(self, pyfuncitem):
        """Observe executed assertion lines and entered expected-refusal contexts."""
        filename = str(Path(pyfuncitem.path).resolve())
        if filename not in self.assertion_lines:
            tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
            self.assertion_lines[filename] = {
                node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Assert) or (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "raises")}
        lines = self.assertion_lines[filename]
        observed = set()
        previous = sys.gettrace()

        def trace(frame, event, arg):
            if frame.f_code.co_filename != filename:
                return None
            if event == "line" and frame.f_lineno in lines:
                observed.add((frame.f_code.co_name, frame.f_lineno))
            return trace

        sys.settrace(trace)
        try:
            yield
        finally:
            sys.settrace(previous)
            self.observed[pyfuncitem.nodeid] = len(observed)
            self.append({"event": "assertions", "nodeid": pyfuncitem.nodeid, "observed": len(observed)})

    def snapshots(self, nodeid):
        """Retain bounded native hashes and public basis/error fields, never keys."""
        refs, total = [], 0
        for tmp in self.temporary_roots.get(nodeid, []):
            for index, path in enumerate(tmp.rglob("*")):
                if index >= MAX_FILES:
                    self.append({"event": "artifact_bound", "nodeid": nodeid})
                    break
                if not path.is_file() or path.is_symlink() or path.suffix not in {".jsonl", ".json"}:
                    continue
                relative = str(path.relative_to(tmp))
                if any(word in relative.lower() for word in _PRIVATE):
                    continue
                if path.suffix == ".json" and not path.name.endswith(".state.json"):
                    continue
                size = path.stat().st_size
                if len(refs) >= 32 or size > MAX_RECORD_BYTES or total + size > 8 * MAX_RECORD_BYTES:
                    self.append({"event": "artifact_bound", "nodeid": nodeid})
                    break
                raw = path.read_bytes()
                total += len(raw)
                try:
                    lines = raw.splitlines() if path.suffix == ".jsonl" else [raw]
                    values = [json.loads(line) for line in lines[-32:]]
                    public = [_public(bounded_json(value, 32768), narrow=True) for value in values]
                except (ValueError, UnicodeError):
                    continue
                public = bounded_json(public, 32768)
                root_relative = str(tmp.relative_to(Path(self.request["basetemp"])))
                before_hash = self.native_before.get(nodeid, {}).get(str(path.relative_to(Path(self.request["basetemp"]))))
                native_hash = hashlib.sha256(raw).hexdigest()
                retained = {"schema_version": "acceptance-native-artifact/v1", "nodeid": nodeid,
                            "run_nonce": self.request["run_nonce"], "capture_pid": os.getpid(),
                            "root_relative": root_relative, "path": relative,
                            "native_sha256": native_hash, "native_bytes": len(raw),
                            "before_native_sha256": before_hash, "changed_since_setup": before_hash != native_hash,
                            "public": public, "public_sha256": digest(public),
                            "scope": {"redacted": True, "projection_truncated": len(lines) > 32,
                                      "native_records": len(lines), "retained_records": len(public),
                                      "full_raw_replay": "unqualified"}}
                target = Path(self.request["artifacts"]) / (digest([nodeid, self.request["run_nonce"], root_relative, relative]) + ".json")
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(canonical_json(retained))
                refs.append({"path": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                             "native_sha256": retained["native_sha256"], "changed_since_setup": retained["changed_since_setup"]})
        self.append({"event": "artifacts", "nodeid": nodeid, "refs": refs})

    def pytest_runtest_logfinish(self, nodeid, location):
        self.snapshots(nodeid)
        self.append({"event": "finished", "nodeid": nodeid})
        self.active = None

    def pytest_sessionfinish(self, session, exitstatus):
        value = {"schema_version": "acceptance-pytest-result/v1", "run_nonce": self.request["run_nonce"],
                 "capture_pid": os.getpid(), "collected": self.collected, "exit_code": int(exitstatus),
                 "collection_errors": self.collection_errors,
                 "package_location": str(sys.modules["evidence_wiki"].__file__) if "evidence_wiki" in sys.modules else None,
                 "plugin_location": str(Path(__file__).resolve()), "framework_location": str(Path(pytest.__file__).resolve())}
        target = Path(self.request["result"])
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(bounded_json(value)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise ValueError("acceptance_worker_request_required")
    request = json.loads(Path(argv[0]).read_bytes())
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    os.chdir(ROOT)
    plugin = AcceptanceRecords(request)
    arguments = ["-q", "--capture=no", "-p", "no:cacheprovider", "--override-ini", "addopts=", "--rootdir", str(ROOT),
                 "-c", str(ROOT / "pyproject.toml"), "--basetemp", request["basetemp"]]
    arguments += ["--collect-only"] if request["mode"] == "collect" else []
    arguments += request["selectors"] if request["mode"] == "collect" else request["exact_nodeids"]
    # Registration under this name is the only optional recording adapter tests use.
    class Registration:
        def pytest_configure(self, config):
            config.pluginmanager.register(plugin, "acceptance-records")
    return int(pytest.main(arguments, plugins=[Registration()]))


if __name__ == "__main__":
    raise SystemExit(main())
