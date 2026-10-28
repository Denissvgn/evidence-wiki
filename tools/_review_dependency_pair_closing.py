"""Fresh native pair closing under a prospectively reserved guarded owner."""

from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from tools import _review_dependency_accounting as accounting


class PairClosingUnavailable(ValueError):
    """Historical completed evidence has no reserved current closing authority."""

    error_code = "accounting_pair_current_closing_unavailable"


def _clock(capability):
    accounting.require(capability._timing == (capability.started, capability.deadline)
                       and capability._plan_bytes == accounting.canonical(capability.plan),
                       "accounting_pair_closing_authority_changed")
    now = datetime.now(timezone.utc)
    accounting.require(datetime.fromisoformat(capability.plan["frozen_at"]) <= now
                       < datetime.fromisoformat(capability.plan["expires_at"])
                       and time.monotonic() < capability.deadline,
                       "accounting_pair_closing_authority_expired")
    return min(capability.deadline - time.monotonic(),
               (datetime.fromisoformat(capability.plan["expires_at"]) - now).total_seconds())


def _executor(capability):
    """Authenticate actual retained final executor objects without polling PIDs."""
    executor = capability.runtime.execution.executor
    from evidence_wiki import BoundedSubprocessExecutor
    accounting.require(type(executor) is BoundedSubprocessExecutor, "accounting_dispatch_owner_invalid")
    with executor._lock:
        rows = []
        for identifier, running in executor._attempts.items():
            accounting.require(identifier == running.attempt.attempt_id and running.finished.is_set()
                               and running.snapshot is not None and running.snapshot.get("final_accounting") is True
                               and running.snapshot.get("state") != "running", "accounting_dispatch_handoff_incomplete")
            observed = executor.authenticate(executor.poll(running.attempt), running.attempt)
            accounting.require(observed["final_accounting"] is True and observed["state"] != "running",
                               "accounting_dispatch_handoff_incomplete")
            # Poll signs the executor's retained completion; it does not poll a
            # process or renew job authority. Only envelope freshness varies.
            final = {key: value for key, value in observed.items()
                     if key not in {"sequence", "observed_utc", "authentication"}}
            rows.append({"attempt": running.attempt.document(), "final": final})
        return accounting.canonical(rows)


def _fresh(capability, originals):
    from tools import run_review_dependency_workload as legacy
    from tools._review_dependency_runner import _capture_completed_mode
    _clock(capability)
    observed, _snapshots, _records = legacy._owner_read(capability.root, capability.runtime,
        [row["native_run_id"] for row in capability.plan["journal_slots"]], originals,
        capability.plan["observation_policy"],
        **({"selective_run_id": capability.plan["obligations"][-1]["native_run_id"]}
           if capability.plan["mode"] == "selective_reuse" else {}))
    accounting.require(all(row["accepted"] for row in observed["strict"]["claims"])
                       and observed["publication"]["verdict"] == "ship", "accounting_pair_owner_failed")
    # Every complete HMAC-authenticated history must match the completed owner.
    from tools._review_dependency_runner import _read
    prior = _read(capability.native["dispatch"]["journal_inventory_pin"])
    stable = lambda rows: [{key: row[key] for key in ("run_id", "records_sha256", "records_bytes", "record_count", "head_ref", "source_sha256", "state_sha256")} for row in rows]
    accounting.require(stable(observed["journal_receipts"]) == stable(prior), "accounting_pair_journal_changed")
    native = _capture_completed_mode(capability.plan, copy.deepcopy(capability.report), capability.runtime,
        capability.root, capability.tracker, capability.fixture, capability.sources, originals,
        SimpleNamespace(document=capability.native["dispatch"]), capability.started, fresh=True)
    accounting.require(native["authority"] == capability.native["authority"], "accounting_pair_authority_changed")
    for coordinate, old in capability.native["sources"].items():
        current = native["sources"][coordinate]
        accounting.require(all(current[key] == old[key] for key in ("source_id", "revision", "record_path", "record_sha256", "files", "descriptor")),
                           "accounting_pair_source_changed")
    _clock(capability)
    return native, observed


def _same_current(native, previous):
    stable = ("producer", "authority", "config", "computation", "questions", "requirements")
    accounting.require(all(native[key] == previous[key] for key in stable)
        and all({key: value for key, value in native[name].items() if key != "evaluated_at"}
                == {key: value for key, value in previous[name].items() if key != "evaluated_at"}
                for name in ("strict", "publication"))
        and all({key: value for key, value in source.items() if key != "usage"}
                == {key: value for key, value in previous["sources"][coordinate].items() if key != "usage"}
                and {key: value for key, value in source["usage"].items() if key != "evaluated_at"}
                == {key: value for key, value in previous["sources"][coordinate]["usage"].items() if key != "evaluated_at"}
                for coordinate, source in native["sources"].items()), "accounting_pair_current_changed")


def _reservation_evidence(modes):
    if modes[0].plan["schema_version"] in {accounting.EXTENDED_PLAN, accounting.COLLECTION_PLAN, accounting.LONG_PLAN, accounting.OWNER_PLAN}:
        return {"schema_version": "review-current-pair-closing/v2",
                "reservations": {cap.plan["mode"]: copy.deepcopy(cap.plan["pair_closing_reservation"]) for cap in modes}}
    return {"schema_version": "review-current-pair-closing/v1", "reservation": accounting.PAIR_RESERVATION}


def require_current_pair_closing(legacy, selective, originals=None, *, consume=None):
    """Keep one retained deadline and guard through replay, final reads and mint."""
    import subprocess

    from tools._review_dependency_runner import _CompletedMode, _mode_members
    from tools._review_dependency_workload import _POPEN_TYPE, OwnedChildren, digest, workload_deadline

    modes = (legacy, selective)
    accounting.require(all(type(cap) is _CompletedMode and cap in _mode_members() for cap in modes)
                       and legacy is not selective, "accounting_completed_mode_owner_required")
    for cap in modes:
        cap.assert_original()
    if any(not hasattr(cap, "plan") or cap.plan.get("schema_version") not in {accounting.PAIR_PLAN, accounting.EXTENDED_PLAN, accounting.COLLECTION_PLAN, accounting.LONG_PLAN, accounting.OWNER_PLAN} for cap in modes):
        raise PairClosingUnavailable(PairClosingUnavailable.error_code)
    accounting.require(originals is not None, "accounting_pair_closing_originals_required")
    accounting.require(callable(consume), "accounting_pair_closing_consumer_required")
    accounting.require(all(getattr(originals, "journal_collection", False) == accounting.collection_selected(cap.plan) for cap in modes),
                       "accounting_journal_collection_policy_changed")
    for cap in modes:
        accounting.validate_plan(cap.plan)
    accounting.require(legacy.plan["schema_version"] == selective.plan["schema_version"],
                       "accounting_pair_plan_binding_invalid")
    accounting.require(all(cap._pair_closing_claimed is False for cap in modes),
                       "accounting_pair_closing_reservation_consumed")
    for cap in modes:
        cap._pair_closing_claimed = True
    tick = time.monotonic()
    seconds = min(*(_clock(cap) for cap in modes),
                  *(cap.plan["pair_closing_reservation"]["pair_close_seconds"] for cap in modes))
    deadline = tick + seconds
    tracker = OwnedChildren(originals, digest({cap.plan["mode"]: cap.plan for cap in modes}))
    tracker.open(predecessor=selective)
    fresh, observations, initial = {}, {}, {}
    try:
        with workload_deadline(deadline - time.monotonic()):
            initial = {cap.plan["mode"]: _executor(cap) for cap in modes}
            for cap in modes:
                native, observed = _fresh(cap, originals)
                fresh[cap.plan["mode"]], observations[cap.plan["mode"]] = native, observed
            # Full native replay and every normalization receipt share this timer.
            mint = consume(fresh)
            accounting.require(callable(mint), "accounting_pair_closing_mint_required")
    finally:
        # This closed guard remains installed through terminal native assertions.
        with workload_deadline(min(cap.plan["phase_caps"]["cleanup"] for cap in modes)):
            cleanup = tracker.close()
            originals.append(("pair-closing-cleanup", cleanup))
    commands = [tuple(command) for cap in modes for command in cap.runtime.execution.executor._commands.values()]
    accounting.require(all(tuple(process.args) not in commands for process in tracker.owned),
                       "accounting_pair_worker_dispatch")
    reservation = _reservation_evidence(modes)
    evidence = {**reservation,
        "mode_native_pins": {mode: originals.append((mode + "-pair-current", native)) for mode, native in fresh.items()},
        "owner_pins": {mode: originals.append((mode + "-pair-owner", observed)) for mode, observed in observations.items()},
        "executor_pins": {mode: originals.append((mode + "-pair-executor", {"authenticated_inventory_sha256": digest(raw.decode())})) for mode, raw in initial.items()},
        "cleanup": cleanup, "additional_worker_dispatches": 0,
        "elapsed_seconds": time.monotonic() - tick,
        "mode_deadlines": {cap.plan["mode"]: {"started_monotonic": cap.started, "deadline_monotonic": cap.deadline,
            "authority_expires_at": cap.plan["expires_at"]} for cap in modes}}
    closing_pin = originals.append(("current-pair-closing", evidence))

    def assert_guard():
        accounting.require(tracker.closed and subprocess.Popen is tracker.launch
            and tracker.real_popen is _POPEN_TYPE
            and all(cap.tracker.real_popen is tracker.real_popen for cap in modes),
            "accounting_pair_closing_guard_changed")

    def assert_current(finalize=None):
        # Never renew the reservation: late differential serialization consumes
        # only the remainder of the same prospectively claimed allocation.
        with workload_deadline(deadline - time.monotonic()):
            assert_guard()
            for cap in modes:
                cap.assert_original()
                _clock(cap)
                native, observed = _fresh(cap, originals)
                _same_current(native, fresh[cap.plan["mode"]])
                observations[cap.plan["mode"]] = observed
                accounting.require(_executor(cap) == initial[cap.plan["mode"]], "accounting_pair_worker_dispatch")
                assert_guard()
            if finalize is not None:
                finalize()
            now = datetime.now(timezone.utc)
            for cap in modes:
                mode = cap.plan["mode"]
                accounting.require(now < datetime.fromisoformat(fresh[mode]["temporal_not_after"])
                    and now < datetime.fromisoformat(observations[mode]["observer_recency_not_after"]),
                    "accounting_pair_temporal_gate_expired")
                _clock(cap)
            assert_guard()
            accounting.require(time.monotonic() < deadline,
                               "accounting_pair_closing_allocation_expired")

    # This provisional normalization capability cannot publish success: the
    # differential owner must invoke its retained live assertion after all
    # result receipts, within this same deadline, before returning a pair pass.
    assert_guard()
    with workload_deadline(deadline - time.monotonic()):
        proof = mint(closing_pin, assert_current)
        # Minting validation itself can consume time; no stale proof escapes.
        now = datetime.now(timezone.utc)
        for cap in modes:
            mode = cap.plan["mode"]
            accounting.require(now < datetime.fromisoformat(fresh[mode]["temporal_not_after"])
                and now < datetime.fromisoformat(observations[mode]["observer_recency_not_after"]),
                "accounting_pair_temporal_gate_expired")
            _clock(cap)
        assert_guard()
        accounting.require(time.monotonic() < deadline, "accounting_pair_closing_allocation_expired")
        return proof
