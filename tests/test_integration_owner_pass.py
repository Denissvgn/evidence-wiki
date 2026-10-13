"""Process scheduling preserves current original-owner decision semantics."""

import json
import os
import signal
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace

import pytest

from tests.test_integration_improvement import NOW, fixture
from tests.test_repeated_integration_host import owner_fixture
from tools import _integration_improvement as gate
from tools import _integration_owner_pass as owners
from tools import read_integration_owner as worker
from tools import replay_qualification_native as native
from tools._release_decision import canonical, digest


def request_for(plan, host, *, nonce="1" * 32):
    original = {host: {"path": "/unused/" + host + "/report.json"}}
    return {"schema_version": "integration-owner-request/v1", "pass_nonce": nonce,
            "plan": plan, "plan_sha256": digest(plan), "host_id": host, "originals": original,
            "originals_sha256": digest(original), "source_manifest": {},
            "slot_ids": sorted(slot["slot_id"] for slot in plan["slots"] if slot["host_id"] == host)}


def encoded_group(request, natives):
    return b"".join(owners._frame(request, sid, evidence=natives[sid]) for sid in request["slot_ids"])


def fake_launch(monkeypatch, commands):
    """Spawn bounded Python emitters; never invoke a native host or factory."""
    processes = []
    def launch(request):
        code = commands[request["host_id"]]
        if isinstance(code, Exception):
            raise code
        code = "import signal;signal.pthread_sigmask(signal.SIG_SETMASK,set());" + code
        process = subprocess.Popen([str(owners.ROOT / ".venv/bin/python"), "-c", code],  # noqa: S603 -- fixed test emitter.
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        os.set_blocking(process.stdout.fileno(), False)
        processes.append(process)
        return process
    monkeypatch.setattr(owners, "_launch", launch)
    return processes


def emit(raw, *, sleep=0):
    return "import sys,time;sys.stdout.buffer.write(" + repr(raw) + ");sys.stdout.flush();time.sleep(" + repr(sleep) + ")"


def assert_clean(processes):
    assert all(process.poll() is not None and not owners._group_alive(process) for process in processes)


def test_native_wire_preserves_original_data_and_times():
    plan, natives, _, _ = fixture()
    request = request_for(plan, "direct")
    sid = request["slot_ids"][0]
    assert owners._decode_frame(owners._frame(request, sid, evidence=natives[sid]), request, sid) == natives[sid]


@pytest.mark.parametrize("change", [
    {"host_id": "scoped_mcp"}, {"request_sha256": "0" * 64}, {"slot_id": "unexpected"},
    {"kind": "passed", "data": True}, {"kind": "evidence", "data": {"authenticated": True}},
    {"kind": "error", "data": {"value_error": False, "integrity_code": "integration_owner_binding"}},
    {"kind": "error", "data": {"value_error": True, "integrity_code": "private_exception_text"}},
])
def test_wire_cannot_appoint_an_owner_or_export_arbitrary_errors(change):
    plan, natives, _, _ = fixture()
    request = request_for(plan, "direct")
    sid = request["slot_ids"][0]
    frame = json.loads(owners._frame(request, sid, evidence=natives[sid])) | change
    with pytest.raises(ValueError):
        owners._decode_frame(canonical(frame), request, sid)


def test_worker_serial_reads_stop_after_first_failure(monkeypatch):
    plan, _, _, readers = fixture()
    request = request_for(plan, "direct")
    calls = []
    def fail():
        calls.append(1)
        raise ValueError("integration_original_history_changed")
    selected = [gate.OwnerReader(reader.slot_id, reader.owner_ref, fail)
                for reader in readers if reader.slot_id in request["slot_ids"]]
    monkeypatch.setattr(worker, "host_readers", lambda *_, **__: selected)
    frames = []
    worker.read_group(request, frames.append)
    results = [owners._decode_frame(frame, request, sid) for frame, sid in zip(frames, request["slot_ids"], strict=True)]
    assert calls == [1]
    assert str(results[0]) == "integration_original_history_changed"
    assert all(type(error) is OSError for error in results[1:])


def test_worker_error_frame_contains_only_allowlisted_integrity_code():
    plan, _, _, _ = fixture()
    request = request_for(plan, "direct")
    sid = request["slot_ids"][0]
    for error in (RuntimeError("private-key-and-factory"), ValueError("private-key-and-factory")):
        raw = owners._frame(request, sid, error=error)
        assert b"private-key-and-factory" not in raw
        assert set(json.loads(raw)["data"]) == {"value_error", "integrity_code"}


def test_completed_unsafe_and_failed_groups_survive_other_timeout(monkeypatch):
    plan, natives, _, _ = fixture({"direct:baseline": {"critical_failure": True}})
    requests = [request_for(plan, host["host_id"]) for host in plan["hosts"]]
    failed = requests[1]
    commands = {"direct": emit(encoded_group(requests[0], natives)),
                "current_caller": emit(owners._frame(failed, failed["slot_ids"][0],
                                          error=ValueError("integration_original_history_changed"))),
                "scoped_mcp": "import time;time.sleep(10)"}
    processes = fake_launch(monkeypatch, commands)
    results, errors, _ = owners._collect(requests, workers=3, deadline=time.monotonic() + 0.5)
    assert results["direct:baseline"].projection["critical_failure"] is True
    assert str(results[failed["slot_ids"][0]]) == "integration_original_history_changed"
    assert all(type(results[sid]) is OSError for sid in requests[2]["slot_ids"])
    assert errors == ()
    assert_clean(processes)


@pytest.mark.parametrize("tail,code", [(b'{"passed":true}\n', "integration_owner_binding"),
                                       (b"incomplete", "integration_original_response_changed")])
def test_malformed_tail_preserves_genuine_unsafe_and_closing_fault(monkeypatch, tail, code):
    plan, natives, _, _ = fixture({"direct:baseline": {"critical_failure": True}})
    request = request_for(plan, "direct")
    processes = fake_launch(monkeypatch, {"direct": emit(encoded_group(request, natives) + tail)})
    results, errors, _ = owners._collect([request], workers=1, deadline=time.monotonic() + 2)
    assert results["direct:baseline"].projection["critical_failure"] is True
    assert code in errors
    assert_clean(processes)


def test_overflow_is_bounded_and_retains_completed_evidence(monkeypatch):
    plan, natives, _, _ = fixture({"direct:baseline": {"history_preserved": False}})
    request = request_for(plan, "direct")
    initial = owners._frame(request, request["slot_ids"][0], evidence=natives[request["slot_ids"][0]])
    code = emit(initial) + ";sys.stdout.buffer.write(b'x'*" + str(owners.MAX_FRAME_BYTES + 65_536) + ");sys.stdout.flush()"
    processes = fake_launch(monkeypatch, {"direct": code})
    results, errors, _ = owners._collect([request], workers=1, deadline=time.monotonic() + 2)
    assert results["direct:baseline"].projection["history_preserved"] is False
    assert "cost_output_overflow" in errors
    assert_clean(processes)


def test_launch_failure_does_not_erase_another_host(monkeypatch):
    plan, natives, _, _ = fixture()
    good, failed = request_for(plan, "direct"), request_for(plan, "scoped_mcp")
    processes = fake_launch(monkeypatch, {"direct": emit(encoded_group(good, natives)), "scoped_mcp": OSError()})
    results, _, _ = owners._collect([failed, good], workers=2, deadline=time.monotonic() + 2)
    assert all(type(results[sid]) is OSError for sid in failed["slot_ids"])
    assert all(results[sid] == natives[sid] for sid in good["slot_ids"])
    assert_clean(processes)


def test_late_frame_is_unknown_and_worker_is_terminated(monkeypatch):
    plan, natives, _, _ = fixture()
    request = request_for(plan, "direct")
    raw = encoded_group(request, natives)
    processes = fake_launch(monkeypatch, {"direct": "import time;time.sleep(.3);" + emit(raw)})
    results, errors, _ = owners._collect([request], workers=1, deadline=time.monotonic() + 0.1)
    assert all(type(results[sid]) is OSError for sid in request["slot_ids"])
    assert errors == ()
    assert_clean(processes)


def test_partial_host_preserves_unsafe_original_when_remaining_reads_timeout(monkeypatch):
    plan, natives, _, _ = fixture({"direct:baseline": {"critical_failure": True}})
    request = request_for(plan, "direct")
    raw = owners._frame(request, request["slot_ids"][0], evidence=natives[request["slot_ids"][0]])
    processes = fake_launch(monkeypatch, {"direct": emit(raw, sleep=10)})
    results, errors, _ = owners._collect([request], workers=1, deadline=time.monotonic() + .3)
    assert results["direct:baseline"].projection["critical_failure"] is True
    assert all(type(results[sid]) is OSError for sid in request["slot_ids"][1:])
    assert not errors
    assert_clean(processes)


def test_frame_from_prior_pass_is_an_integrity_fault(monkeypatch):
    plan, natives, _, _ = fixture()
    old = request_for(plan, "direct", nonce="1" * 32)
    current = request_for(plan, "direct", nonce="2" * 32)
    processes = fake_launch(monkeypatch, {"direct": emit(encoded_group(old, natives))})
    results, errors, _ = owners._collect([current], workers=1, deadline=time.monotonic() + 2)
    assert all(type(results[sid]) is ValueError for sid in current["slot_ids"])
    assert errors == ("integration_owner_binding",)
    assert_clean(processes)


def test_one_process_per_distinct_host_and_bounded_live_count(monkeypatch):
    plan, natives, _, _ = fixture()
    requests = [request_for(plan, host["host_id"]) for host in plan["hosts"]]
    processes = fake_launch(monkeypatch, {r["host_id"]: emit(encoded_group(r, natives), sleep=.1) for r in requests})
    original = owners._launch
    counts = []
    def launch(request):
        counts.append(sum(process.poll() is None for process in processes) + 1)
        return original(request)
    monkeypatch.setattr(owners, "_launch", launch)
    results, errors, _ = owners._collect(requests, workers=2, deadline=time.monotonic() + 3)
    assert len(processes) == 3 and max(counts) == 2
    assert len(results) == 9 and not errors
    assert_clean(processes)


def controller_fixture(monkeypatch, *, workers=3, overrides=None):
    plan, natives, pins, readers = fixture(overrides)
    candidate = {"candidate": "fixed"}
    plan["candidate_sha256"] = digest(candidate)
    plan["scope"].update(owner_read_workers=workers, owner_read_controller_sha256=digest({}))
    natives = {sid: replace(native, candidate_sha256=plan["candidate_sha256"]) for sid, native in natives.items()}
    originals = {host["host_id"]: {"path": "/unused/" + host["host_id"], "raw_sha256": "1" * 64,
                 "source_head_sha256": "2" * 64, "observation": {}} for host in plan["hosts"]}
    monkeypatch.setattr(owners, "source_manifest", lambda: {})
    monkeypatch.setattr(owners, "_isolated_roots", lambda _: None)
    monkeypatch.setattr("tools.verify_acceptance_paths.candidate_manifest", lambda _: candidate)
    controller = owners.NativeOwnerPass(plan, originals, remaining_seconds=30)
    return plan, natives, pins, readers, controller


def test_controller_builds_two_fresh_passes_and_replay_uses_them(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    passes, times = [], []
    def collect(requests, **_):
        passes.append(tuple(r["pass_nonce"] for r in requests))
        times.append(time.monotonic())
        # Re-observation changes only genuine timestamps, never raw originals.
        return {sid: replace(native, observed_at="2030-01-01T11:59:00Z") for sid, native in natives.items()}, (), ()
    monkeypatch.setattr(owners, "_collect", collect)
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "continue" and len(passes) == 2
    assert len(set(sum(passes, ()))) == 6
    assert gate.replay_decision(result, digest(plan), digest(pins), controller.readers, NOW,
                                prepare_pass=controller.prepare_pass, closing_check=controller.closing_check) == result
    assert len(passes) == 4 and len(set(sum(passes, ()))) == 12


def test_cleanup_failure_blocks_later_passes(monkeypatch):
    _, _, _, _, controller = controller_fixture(monkeypatch)
    calls = []
    def broken(*_, **__):
        calls.append(1)
        raise RuntimeError("integration_owner_cleanup_unconfirmed")
    monkeypatch.setattr(owners, "_collect", broken)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="cleanup_unconfirmed"):
            controller.prepare_pass()
    assert calls == [1]
    with pytest.raises(RuntimeError, match="cleanup_unconfirmed"):
        controller.closing_check()


def test_malformed_stream_stop_is_monotone_through_second_pass(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    calls = []
    def collect(*_, **__):
        calls.append(1)
        return natives, (("integration_owner_binding",) if len(calls) == 1 else ()), ()
    monkeypatch.setattr(owners, "_collect", collect)
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "stop"
    assert any(reason["code"] == "evidence_integrity_failure" for reason in result["reasons"])


def test_global_close_time_invalidates_prepared_originals(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    monkeypatch.setattr(owners, "_collect", lambda *_, **__: (natives, (), ()))
    late = datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    clocks = iter([NOW] * 19 + [late] * 20)
    monkeypatch.setattr(gate, "_clock", lambda _: next(clocks))
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "revise"
    assert all(row["problem"] == "owner_expired_at_close" for row in result["slots"])


def test_preparation_does_not_renew_expired_owner_times(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    expired = {sid: replace(native, expires_at="2030-01-01T12:00:00Z") for sid, native in natives.items()}
    monkeypatch.setattr(owners, "_collect", lambda *_, **__: (expired, (), ()))
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "revise" and result["metrics"]["accepted_slots"] == 0


def test_current_second_pass_cannot_replace_changed_original(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    calls = []
    def collect(*_, **__):
        calls.append(1)
        result = dict(natives)
        if len(calls) == 2:
            result["direct:baseline"] = replace(result["direct:baseline"], raw=canonical({"changed": True}))
        return result, (), ()
    monkeypatch.setattr(owners, "_collect", collect)
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "stop"
    assert any(reason["code"] == "evidence_integrity_failure" for reason in result["reasons"])


@pytest.mark.parametrize("workers", [True, False, 0, 4, "3", 1.5])
def test_scheduler_requires_exact_integer_bound(monkeypatch, workers):
    with pytest.raises(ValueError, match="integration_owner_worker_bound"):
        controller_fixture(monkeypatch, workers=workers)


def test_controller_pin_and_candidate_are_parent_owned(monkeypatch):
    plan, _, _, _, controller = controller_fixture(monkeypatch)
    monkeypatch.setattr(owners, "source_manifest", lambda: {"changed": "1" * 64})
    controller.prepare_pass()
    with pytest.raises(ValueError, match="integration_current_candidate_changed"):
        controller.closing_check()
    with pytest.raises(ValueError, match="integration_current_candidate_changed"):
        owners.NativeOwnerPass(plan, controller.originals, remaining_seconds=30)


def test_default_generic_reading_remains_serial_without_callback():
    plan, natives, pins, readers = fixture()
    calls = []
    readers = [gate.OwnerReader(r.slot_id, r.owner_ref,
                 lambda sid=r.slot_id: (calls.append(sid), natives[sid])[1]) for r in reversed(readers)]
    result = gate.decide(plan, digest(plan), pins, digest(pins), readers, NOW)
    assert result["outcome"] == "continue"
    assert calls == sorted(natives) * 2


def test_actual_launch_uses_fixed_argv_and_private_bounded_input(monkeypatch):
    request = {"small": "selection"}
    seen = {}
    class Output:
        def fileno(self):
            return 123
    class Process:
        stdout = Output()
    def popen(argv, **kwargs):
        source = kwargs["stdin"]
        seen.update(argv=argv, payload=source.read(), mode=os.fstat(source.fileno()).st_mode & 0o777,
                    cwd=kwargs["cwd"], session=kwargs["start_new_session"])
        return Process()
    monkeypatch.setattr(owners.subprocess, "Popen", popen)
    monkeypatch.setattr(owners.os, "set_blocking", lambda *_: None)
    owners._launch(request)
    assert seen["argv"] == [str(owners.ROOT / ".venv/bin/python"), str(owners.ROOT / "tools/read_integration_owner.py")]
    assert seen["payload"] == canonical(request) and seen["mode"] == 0o600
    assert seen["session"] is True and seen["cwd"] == owners.ROOT
    with pytest.raises(ValueError):
        owners._launch({"overflow": "x" * owners.MAX_REQUEST_BYTES})


def test_all_positive_frames_from_unclosed_worker_cannot_qualify(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    requests = [request_for(plan, host["host_id"]) for host in plan["hosts"]]
    processes = fake_launch(monkeypatch, {r["host_id"]: emit(encoded_group(r, natives), sleep=10) for r in requests})
    results, errors, incomplete = owners._collect(requests, workers=3, deadline=time.monotonic() + .3)
    assert all(type(value) is gate.OwnerEvidence for value in results.values())
    assert set(incomplete) == set(gate.ROUTES)
    assert not errors
    monkeypatch.setattr(owners, "_collect", lambda *_, **__: (results, errors, incomplete))
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "revise"
    assert result["metrics"]["accepted_slots"] == 0
    assert result["next_improvement"] is None
    assert all(row["problem"] == "owner_unavailable" for row in result["slots"])
    assert "closing_owner_unavailable" in {reason["code"] for reason in result["reasons"]}
    assert_clean(processes)


def test_unclosed_group_still_preserves_all_unsafe_evidence(monkeypatch):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch)
    natives["direct:baseline"] = replace(natives["direct:baseline"], raw=b"{}")
    monkeypatch.setattr(owners, "_collect", lambda *_, **__: (natives, (), tuple(gate.ROUTES)))
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "stop" and result["metrics"]["accepted_slots"] == 0
    assert any(reason["code"] == "evidence_integrity_failure" for reason in result["reasons"])


@pytest.mark.parametrize("changes,code", [({"critical_failure": True}, "unsafe_invariant"),
                                          ({"stale_approval_reused": True}, "unsafe_invariant"),
                                          ({"history_preserved": False}, "unsafe_invariant"),
                                          ({"elapsed_seconds": "11"}, "elapsed_budget_exceeded")])
def test_unclosed_group_preserves_exact_unsafe_citations(monkeypatch, changes, code):
    plan, natives, pins, _, controller = controller_fixture(monkeypatch, overrides={"direct:baseline": changes})
    monkeypatch.setattr(owners, "_collect", lambda *_, **__: (natives, (), tuple(gate.ROUTES)))
    result = gate.decide(plan, digest(plan), pins, digest(pins), controller.readers, NOW,
                         prepare_pass=controller.prepare_pass, closing_check=controller.closing_check)
    assert result["outcome"] == "stop" and result["metrics"]["accepted_slots"] == 0
    assert any(reason["code"] == code and reason["citations"] for reason in result["reasons"])


@pytest.mark.parametrize("prepared", [True, [], {}, {"direct:baseline"}, frozenset({"not-planned"})])
def test_prepare_availability_cannot_grant_or_select_unplanned_evidence(prepared):
    plan, _, pins, readers = fixture()
    with pytest.raises(ValueError, match="integration_prepare_pass"):
        gate.decide(plan, digest(plan), pins, digest(pins), readers, NOW, prepare_pass=lambda: prepared)


def test_nonzero_worker_cannot_qualify_its_positive_frames(monkeypatch):
    plan, natives, _, _ = fixture()
    request = request_for(plan, "direct")
    processes = fake_launch(monkeypatch, {"direct": emit(encoded_group(request, natives)) + ";sys.exit(2)"})
    results, errors, incomplete = owners._collect([request], workers=1, deadline=time.monotonic() + 2)
    assert all(type(value) is gate.OwnerEvidence for value in results.values())
    assert incomplete == ("direct",) and not errors
    assert_clean(processes)


def test_signal_during_spawn_is_delivered_only_after_owned_tracking(monkeypatch):
    plan, _, _, _ = fixture()
    request = request_for(plan, "direct")
    processes = fake_launch(monkeypatch, {"direct": "import time;time.sleep(10)"})
    original = owners._launch
    def launch(request):
        process = original(request)
        os.kill(os.getpid(), signal.SIGALRM)
        return process
    monkeypatch.setattr(owners, "_launch", launch)
    previous = signal.getsignal(signal.SIGALRM)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    def interrupt(*_):
        raise RuntimeError("cancelled")
    signal.signal(signal.SIGALRM, interrupt)
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            owners._collect([request], workers=1, deadline=time.monotonic() + 2)
        assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
        assert_clean(processes)
    finally:
        signal.signal(signal.SIGALRM, previous)


def test_post_spawn_setup_failure_cleans_before_returning_error(monkeypatch):
    real_popen = subprocess.Popen
    processes = []
    def popen(*_, **kwargs):
        process = real_popen([str(owners.ROOT / ".venv/bin/python"), "-c",  # noqa: S603 -- fixed test emitter.
                             "import signal,time;signal.pthread_sigmask(signal.SIG_SETMASK,set());time.sleep(10)"],
                            **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(owners.subprocess, "Popen", popen)
    def invalid(*_):
        raise ValueError("setup_failed")
    monkeypatch.setattr(owners.os, "set_blocking", invalid)
    with pytest.raises(ValueError, match="setup_failed"):
        owners._launch({"selection": "bounded"})
    assert_clean(processes)


def test_private_input_close_failure_cleans_spawned_worker(monkeypatch):
    real_popen, real_temporary = subprocess.Popen, owners.tempfile.TemporaryFile
    processes = []
    class Input:
        def __init__(self):
            self.source = real_temporary()
        def __enter__(self):
            return self.source
        def __exit__(self, *_):
            self.source.close()
            raise OSError("input_close_failed")
    def popen(*_, **kwargs):
        process = real_popen([str(owners.ROOT / ".venv/bin/python"), "-c",  # noqa: S603 -- fixed test emitter.
                             "import signal,time;signal.pthread_sigmask(signal.SIG_SETMASK,set());time.sleep(10)"],
                            **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(owners.subprocess, "Popen", popen)
    monkeypatch.setattr(owners.tempfile, "TemporaryFile", Input)
    with pytest.raises(OSError, match="input_close_failed"):
        owners._launch({"selection": "bounded"})
    assert_clean(processes)


def test_worker_rejects_stale_source_pin_before_any_owner_read(monkeypatch):
    plan, _, _, _, _ = controller_fixture(monkeypatch)
    request = request_for(plan, "direct")
    monkeypatch.setattr(worker, "source_manifest", lambda: {"stale": "2" * 64})
    with pytest.raises(ValueError, match="integration_current_candidate_changed"):
        worker._request(canonical(request))


def original_at(directory):
    directory.mkdir(parents=True, exist_ok=True)
    fixture = directory / "fixture"
    fixture.mkdir()
    for name in ("workspace", "fixture-private", "fixture-control"):
        (fixture / name).mkdir()
    report = {"workspace": str(fixture / "workspace"), "factory": str(fixture / "fixture-private/services.py"),
              "failure": None}
    raw = canonical(report)
    path = directory / "report.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return {"path": str(path), "raw_sha256": sha256(raw).hexdigest(), "source_head_sha256": digest(report), "observation": {}}


def test_distinct_canonical_fixture_roots_can_be_scheduled(tmp_path):
    originals = {"one": original_at(tmp_path / "one"), "two": original_at(tmp_path / "two")}
    owners._isolated_roots(originals)


@pytest.mark.parametrize("nested", [False, True])
def test_shared_or_nested_host_roots_are_rejected_before_concurrency(tmp_path, nested):
    original = original_at(tmp_path / "outer")
    other = original_at(tmp_path / "outer/inner") if nested else original
    with pytest.raises(ValueError, match="integration_owner_binding"):
        owners._isolated_roots({"one": original, "two": other})


def test_symlinked_workspace_alias_is_rejected(tmp_path):
    original = original_at(tmp_path / "one")
    workspace = tmp_path / "one/fixture/workspace"
    workspace.rmdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    workspace.symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError, match="integration_original_native_paths"):
        owners._isolated_roots({"one": original})


def test_report_selected_for_isolation_must_match_original_pin(tmp_path):
    original = original_at(tmp_path / "one")
    original["raw_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="integration_original_head_changed"):
        owners._isolated_roots({"one": original})


def test_actual_native_runtime_control_guard_precedes_all_workspace_or_probe_work(monkeypatch, tmp_path):
    fixture = tmp_path / "fixture"
    control = fixture / "fixture-control"
    control.mkdir(parents=True)
    runtime = SimpleNamespace(context=SimpleNamespace(mutation_control_directory=tmp_path / "shared-control"))
    monkeypatch.setattr(native.HostRuntime, "from_factory", lambda *_: runtime)
    calls = []
    def forbidden(*_, **__):
        calls.append(1)
        raise AssertionError("guard must precede current work")
    monkeypatch.setattr(native.Workspace, "open", forbidden)
    monkeypatch.setattr("evidence_wiki.calibration_probes.refresh", forbidden)
    with pytest.raises(ValueError, match="integration_current_binding_changed"):
        native.replay(fixture / "workspace", fixture / "fixture-private/services.py", "run",
                      expected_control_root=control)
    assert not calls


def test_first_restored_owner_control_guard_precedes_records_and_native_replay(monkeypatch, tmp_path):
    data = owner_fixture(monkeypatch, tmp_path)
    path = tmp_path / "original/fixture/fixture-control"
    path.mkdir(parents=True)
    calls = []
    def forbidden(*_, **__):
        calls.append(1)
        raise AssertionError("guard must precede current work")
    monkeypatch.setattr(native.Workspace, "open", forbidden)
    selected = data.plan["hosts"][0]["host_id"]
    readers = worker.host_readers(data.plan, {selected: data.selection}, expected_control_root=path)
    with pytest.raises(ValueError, match="integration_current_binding_changed"):
        readers[0].read()
    assert not calls and not data.calls


def test_process_group_cleanup_confirms_ordinary_descendant(tmp_path):
    child_pid = tmp_path / "child.pid"
    # The leader reaps its child on TERM. This verifies group signalling and
    # termination confirmation without leaving an orphan or native process.
    code = ("import subprocess,signal,time,pathlib,sys;"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "pathlib.Path(" + repr(str(child_pid)) + ").write_text(str(child.pid));"
            "signal.signal(signal.SIGTERM,lambda *_:(child.wait(timeout=2),sys.exit(0)));time.sleep(30)")
    process = subprocess.Popen([str(owners.ROOT / ".venv/bin/python"), "-c", code],  # noqa: S603 -- fixed test emitter.
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        limit = time.monotonic() + 2
        while not child_pid.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        assert child_pid.exists()
        owners._stop_group(process)
        assert not owners._group_alive(process) and process.poll() is not None
    finally:
        if process.poll() is None:
            owners._stop_group(process)
