"""Diagnostic scope, content exclusion, timing reconciliation and hook custody."""

import contextlib
import copy
import json
import os
import signal
import sys
import threading

import pytest

from evidence_wiki.mutation_contracts import canonical
from tools import _native_cost_profile as costs

pytestmark = pytest.mark.skipif(
    not (hasattr(sys, "monitoring") and hasattr(sys.monitoring, "clear_tool_id")),
    reason="Local diagnostic monitoring requires Python 3.14 tool cleanup API",
)


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 10
        return self.value


def select(monkeypatch, *functions):
    labels = costs.LABELS[:len(functions)]
    targets = {id(function.__code__): (function.__code__, label)
               for function, label in zip(functions, labels, strict=True)}
    monkeypatch.setattr(costs, "_targets", lambda: (targets, []))
    return labels


def rows(report):
    return {row["label"]: row for row in report["rows"]}


def test_nested_calls_reconcile_through_unselected_code(monkeypatch):
    def leaf():
        return b"private-result"

    def unselected():
        return leaf()

    def parent():
        return unselected()

    parent_label, leaf_label = select(monkeypatch, parent, leaf)
    with costs.NativeCostProfile(clock=Clock()) as profile:
        assert parent() == b"private-result"
    report = profile.report()
    measured = rows(report)
    assert measured[parent_label]["inclusive_ns"] == 60
    assert measured[parent_label]["exclusive_ns"] == 40
    assert measured[leaf_label]["exclusive_ns"] == 20
    assert measured[leaf_label]["observed_bytes"] == len(b"private-result")
    assert report["selected_root_inclusive_ns"] == report["selected_exclusive_ns"] == 60
    assert report["reconciliation"]["exclusive_equals_selected_roots"] is True
    assert "private-result" not in json.dumps(report)


def test_contextmanager_segments_exclude_suspended_body_and_resume(monkeypatch):
    clock = Clock()

    @contextlib.contextmanager
    def segment():
        yield {"confidential": "evidence"}

    def parent():
        with segment():
            clock.value += 10_000

    parent_label, segment_label = select(monkeypatch, parent, segment.__wrapped__)
    with costs.NativeCostProfile(clock=clock) as profile:
        parent()
    measured = rows(profile.report())
    assert measured[segment_label]["call_events"] == 2
    assert measured[segment_label]["completed_segments"] == 2
    assert measured[segment_label]["inclusive_ns"] == 40
    assert measured[parent_label]["exclusive_ns"] >= 10_000
    assert measured[segment_label]["observed_items"] == 1
    assert profile.report()["reconciliation"]["exclusive_equals_selected_roots"] is True


def test_generator_throw_and_unwind_preserve_active_segment_accounting(monkeypatch):
    @contextlib.contextmanager
    def segment():
        yield None

    def parent():
        with segment():
            raise Deadline("private-generator-cancellation")

    parent_label, segment_label = select(monkeypatch, parent, segment.__wrapped__)
    with pytest.raises(Deadline), costs.NativeCostProfile(clock=Clock()) as profile:
        parent()
    report = profile.report()
    measured = rows(report)
    assert measured[segment_label]["call_events"] == 2
    assert measured[segment_label]["completed_segments"] == 2
    assert measured[parent_label]["completed_segments"] == 1
    assert report["reconciliation"]["exclusive_equals_selected_roots"] is True
    assert report["reconciliation"]["stack_mismatches"] == 0
    assert "private-generator-cancellation" not in json.dumps(report)


def test_real_targets_preserve_identity_pins_content_and_input():
    value = {"sensitive-key": ["private-evidence", "signing-key"]}
    before = copy.deepcopy(value)
    original = canonical
    with costs.NativeCostProfile() as profile:
        result = canonical(value)
    report = profile.report()
    assert canonical is original and value == before
    assert report["qualification_granted"] is False
    assert len(report["instrumented_targets"]) == len(costs.LABELS)
    assert all(len(pin["source_sha256"]) == 64 for pin in report["instrumented_targets"])
    assert rows(report)["serialization.canonical"]["observed_bytes"] == len(result)
    encoded = json.dumps(report)
    assert all(token not in encoded for token in ("sensitive-key", "private-evidence", "signing-key"))
    report["rows"][0]["call_events"] = -1
    assert profile.report()["rows"][0]["call_events"] >= 0
    assert sys.getprofile() is None


def test_exception_restores_hook_and_retains_no_exception_content(monkeypatch):
    def refused():
        raise KeyboardInterrupt("private-error-detail")

    label, = select(monkeypatch, refused)
    profile = costs.NativeCostProfile()
    with pytest.raises(KeyboardInterrupt, match="private-error-detail"), profile:
        refused()
    assert sys.getprofile() is None
    assert rows(profile.report())[label]["completed_segments"] == 1
    assert "private-error-detail" not in json.dumps(profile.report())


def test_existing_hook_is_rejected_without_displacing_it():
    def previous(_frame, _event, _arg):
        pass

    sys.setprofile(previous)
    try:
        with pytest.raises(RuntimeError, match="hook_occupied"):
            with costs.NativeCostProfile():
                pass
        assert sys.getprofile() is previous
    finally:
        sys.setprofile(None)


def test_nested_profiles_preserve_outer_hook():
    with costs.NativeCostProfile() as outer:
        hook = sys.getprofile()
        with pytest.raises(RuntimeError, match="hook_occupied"):
            with costs.NativeCostProfile():
                pass
        assert sys.getprofile() == hook
    assert outer.report()["reconciliation"]["hook_failed"] is False
    assert sys.getprofile() is None


def test_depth_and_event_limits_stop_retention_without_affecting_work(monkeypatch):
    def recurse(depth):
        return recurse(depth - 1) + 1 if depth else 0

    label, = select(monkeypatch, recurse)
    with costs.NativeCostProfile(max_depth=2) as depth_profile:
        assert recurse(12) == 12
    report = depth_profile.report()
    assert report["bounds"]["max_retained_depth"] == 2
    assert report["bounds"]["depth_overflow_call_events"] == 11
    assert rows(report)[label]["call_events"] == 13
    assert rows(report)[label]["completed_segments"] == 2
    assert report["reconciliation"]["exclusive_equals_selected_roots"] is True
    with costs.NativeCostProfile(max_events=3) as limited:
        assert recurse(12) == 12
        assert sys.getprofile() is None
    assert limited.report()["bounds"]["observed_events"] == 3
    assert limited.report()["bounds"]["event_limit_reached"] is True
    assert limited.report()["bounds"]["unfinished_segments"] == 3
    assert sys.getprofile() is None


def test_clock_failure_is_diagnostic_and_does_not_replace_result(monkeypatch):
    def operation():
        return 17

    select(monkeypatch, operation)
    clock = Clock()

    def faulty():
        if clock.value >= 10:
            raise RuntimeError("private-clock-error")
        return clock()

    with costs.NativeCostProfile(clock=faulty) as profile:
        assert operation() == 17
    assert sys.getprofile() is None
    assert profile.report()["reconciliation"]["hook_failed"] is True
    assert "private-clock-error" not in json.dumps(profile.report())


class Deadline(BaseException):
    pass


@pytest.mark.parametrize("exception_type", [Deadline, SystemExit, KeyboardInterrupt])
def test_control_exceptions_inside_hook_restore_custody_and_propagate(monkeypatch, exception_type):
    def operation():
        return 17

    select(monkeypatch, operation)
    clock = Clock()
    raised = exception_type("private-control-detail")

    def interrupted():
        if clock.value >= 10:
            raise raised
        return clock()

    profile = costs.NativeCostProfile(clock=interrupted)
    with pytest.raises(exception_type) as caught, profile:
        operation()
    assert caught.value is raised
    assert sys.getprofile() is None
    assert profile.report()["reconciliation"]["hook_failed"] is True
    assert profile.report()["reconciliation"]["exit_clock_observed"] is False
    assert "private-control-detail" not in json.dumps(profile.report())


def test_exceptional_exit_does_not_mask_existing_control_exception():
    clock = Clock()
    raised = Deadline("native-deadline")

    def closing_clock():
        if clock.value >= 10:
            raise SystemExit("secondary-clock-exception")
        return clock()

    profile = costs.NativeCostProfile(clock=closing_clock)
    with pytest.raises(Deadline) as caught, profile:
        raise raised
    assert caught.value is raised
    assert sys.getprofile() is None
    assert profile.report()["reconciliation"]["exit_clock_observed"] is False


def test_clock_regressions_are_clamped_and_disclosed(monkeypatch):
    def operation():
        return None

    select(monkeypatch, operation)
    readings = iter((100, 90, 80, 120, 110, 130))
    with costs.NativeCostProfile(clock=lambda: next(readings)) as profile:
        operation()
    report = profile.report()
    assert report["reconciliation"]["clock_regressions"] == 3
    assert report["selected_root_inclusive_ns"] == 20
    assert report["selected_exclusive_ns"] == 20


def test_builtin_type_checks_do_not_invoke_result_protocols(monkeypatch):
    class SensitiveList(list):
        def __len__(self):
            raise AssertionError("diagnostics invoked caller protocol")

    def operation():
        return SensitiveList(["private"])

    label, = select(monkeypatch, operation)
    with costs.NativeCostProfile() as profile:
        operation()
    assert rows(profile.report())[label]["container_results"] == 0


def test_other_threads_remain_unobserved(monkeypatch):
    def operation():
        return b"thread-data"

    label, = select(monkeypatch, operation)
    with costs.NativeCostProfile() as profile:
        thread = threading.Thread(target=operation)
        thread.start()
        thread.join()
    assert rows(profile.report())[label]["call_events"] == 0
    assert threading.getprofile() is None


def test_monitoring_cleanup_leaves_no_callbacks_or_events_on_owned_slot(monkeypatch):
    def operation():
        return b"measured"

    select(monkeypatch, operation)
    with costs.NativeCostProfile() as profile:
        slot = profile._tool_id
        assert sys.monitoring.get_events(slot) == (sys.monitoring.events.PY_UNWIND | sys.monitoring.events.PY_THROW)
        assert sys.monitoring.get_local_events(slot, operation.__code__) != 0
        operation()
    assert sys.monitoring.get_tool(slot) is None
    assert sys.monitoring.get_events(slot) == 0
    assert sys.monitoring.get_local_events(slot, operation.__code__) == 0
    sys.monitoring.use_tool_id(slot, "cleanup-verification")
    try:
        for event in profile._monitor_events:
            assert sys.monitoring.register_callback(slot, event, None) is None
    finally:
        sys.monitoring.free_tool_id(slot)


def test_monitoring_scope_conflicts_across_threads_without_displacing_owner():
    observed = []

    def concurrent():
        try:
            with costs.NativeCostProfile():
                observed.append("unexpected-entry")
        except RuntimeError as error:
            observed.append(str(error))

    with costs.NativeCostProfile() as profile:
        slot = profile._tool_id
        thread = threading.Thread(target=concurrent)
        thread.start()
        thread.join()
        assert sys.monitoring.get_tool(slot) == "native-cost-profile"
    assert observed == ["native_profile_hook_occupied"]
    assert sys.monitoring.get_tool(slot) is None


def test_monitoring_partial_install_control_exception_cleans_owned_slot(monkeypatch):
    original = sys.monitoring.set_local_events
    observed, slots = [], []

    def interrupted(slot, code, events):
        slots.append(slot)
        original(slot, code, events)
        observed.append(code)
        if len(observed) == 2:
            raise Deadline("private-install-cancellation")

    with monkeypatch.context() as patch:
        patch.setattr(sys.monitoring, "set_local_events", interrupted)
        with pytest.raises(Deadline):
            with costs.NativeCostProfile():
                pass
    assert sys.monitoring.get_tool(slots[0]) is None
    assert all(sys.monitoring.get_local_events(slots[0], code) == 0 for code in observed)
    with costs.NativeCostProfile():
        pass  # The scope lock was also released after partial installation.


def test_no_monitoring_slot_displaces_other_tools():
    owned = []
    try:
        for slot in range(6):
            if sys.monitoring.get_tool(slot) is None:
                sys.monitoring.use_tool_id(slot, "other-test-tool")
                owned.append(slot)
        with pytest.raises(RuntimeError, match="tool_ids_occupied"):
            with costs.NativeCostProfile():
                pass
        assert all(sys.monitoring.get_tool(slot) == "other-test-tool" for slot in owned)
    finally:
        for slot in owned:
            sys.monitoring.free_tool_id(slot)
    with costs.NativeCostProfile():
        pass


def test_cleanup_interruption_releases_tool_and_preserves_active_control_exception(monkeypatch):
    original = sys.monitoring.set_local_events
    raised = KeyboardInterrupt("native-cancellation")

    def interrupted(slot, code, events):
        if events == 0:
            raise Deadline("secondary-cleanup-deadline")
        original(slot, code, events)

    profile = costs.NativeCostProfile()
    with monkeypatch.context() as patch:
        patch.setattr(sys.monitoring, "set_local_events", interrupted)
        with pytest.raises(KeyboardInterrupt) as caught, profile:
            slot = profile._tool_id
            raise raised
    assert caught.value is raised
    assert sys.monitoring.get_tool(slot) is None
    assert sys.monitoring.get_events(slot) == 0
    assert profile.report()["reconciliation"]["hook_failed"] is True
    sys.monitoring.use_tool_id(slot, "cleanup-verification")
    try:
        for event in profile._monitor_events:
            assert sys.monitoring.register_callback(slot, event, None) is None
    finally:
        sys.monitoring.free_tool_id(slot)
    with costs.NativeCostProfile():
        pass


def test_native_alarm_is_delivered_after_all_local_masks_and_ownership_are_cleared(monkeypatch):
    original = sys.monitoring.set_local_events
    seen, requested = [], []
    profile = costs.NativeCostProfile()

    def alarm(_signal, _frame):
        seen.append((sys.monitoring.get_tool(slot),
                     [sys.monitoring.get_local_events(slot, code) for code, _label in profile._codes.values()],
                     costs._MONITOR_LOCK.locked()))
        raise Deadline("native-cleanup-deadline")

    def clearing(tool_id, code, events):
        if events == 0 and not requested:
            requested.append(True)
            os.kill(os.getpid(), signal.SIGALRM)
        original(tool_id, code, events)

    previous = signal.signal(signal.SIGALRM, alarm)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(sys.monitoring, "set_local_events", clearing)
            with pytest.raises(Deadline, match="native-cleanup-deadline"), profile:
                slot = profile._tool_id
    finally:
        signal.signal(signal.SIGALRM, previous)
    assert seen == [(None, [0] * len(costs.LABELS), False)]
    assert profile.report()["reconciliation"]["hook_failed"] is True
    with costs.NativeCostProfile():
        pass


def test_expensive_profile_backend_requires_explicit_selection_and_reports_it(monkeypatch):
    def operation():
        return b"observed"

    label, = select(monkeypatch, operation)
    with costs.NativeCostProfile(backend="profile") as profile:
        assert sys.getprofile() is not None
        operation()
    assert profile.report()["backend"] == "profile"
    assert rows(profile.report())[label]["call_events"] == 1
    assert sys.getprofile() is None


def test_unavailable_atomic_monitoring_is_disclosed(monkeypatch):
    monkeypatch.delattr(sys.monitoring, "clear_tool_id")
    with pytest.raises(RuntimeError, match="monitoring_unavailable"):
        with costs.NativeCostProfile():
            pass
    assert sys.getprofile() is None


@pytest.mark.parametrize("options", [{"max_depth": 0}, {"max_depth": True}, {"max_events": 0},
                                      {"max_events": costs.MAX_EVENTS + 1}, {"clock": None}])
def test_configuration_bounds(options):
    with pytest.raises(ValueError):
        costs.NativeCostProfile(**options)


def test_report_requires_closed_scope_and_scope_is_single_use():
    profile = costs.NativeCostProfile()
    with pytest.raises(RuntimeError, match="not_closed"):
        profile.report()
    with profile:
        with pytest.raises(RuntimeError, match="not_closed"):
            profile.report()
    with pytest.raises(RuntimeError, match="single_use"):
        with profile:
            pass
