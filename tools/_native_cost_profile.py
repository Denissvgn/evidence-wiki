"""Opt-in, bounded current-thread timing diagnostics for native control owners.

The profile hook observes code objects without replacing functions or changing
qualification. No frames, arguments, evidence, exceptions or result contents
are retained. Generator call events are active segments, including resumptions;
time spent suspended is attributed to the selected caller, not the generator.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import sys
import threading
import time
from pathlib import Path

from tools._integration_owner_pass import _defer_signals

# Labels and targets are closed; caller data never creates a reporting dimension.
_TARGETS = (
    ("journal.records", "research_execution", "ResearchExecution.records"),
    ("journal.verify", "research_execution", "ResearchExecution._verify"),
    ("journal.ledger", "research_execution", "ResearchExecution.ledger"),
    ("accounting.authenticate", "execution_accounting", "authenticate_plan"),
    ("accounting.envelope", "execution_accounting", "_authenticate"),
    ("accounting.projection", "execution_accounting", "projection"),
    ("accounting.report", "execution_accounting", "report"),
    ("accounting.reserve", "execution_accounting", "reserve_attempt"),
    ("accounting.observe", "execution_accounting", "observe_attempt"),
    ("review.snapshot", "review_page_operations", "snapshot"),
    ("review.prepare", "review_page_operations", "prepare"),
    ("review.current", "review_page_operations", "ProtectedPages.current"),
    ("review.header", "review_page_operations", "ProtectedPages.header"),
    ("review.page", "review_page_operations", "ProtectedPages.page"),
    ("review.complete", "review_page_operations", "ProtectedPages.complete"),
    ("review.assembly", "review_page_session", "assembled"),
    ("review.current_assembly", "review_packet_execution", "current_assembly"),
    ("current.revision", "mutation_store", "capture_revision"),
    ("current.diagnostics", "claim_diagnostics", "current"),
    ("current.coverage", "coverage_observation", "observe"),
    ("serialization.canonical", "mutation_contracts", "canonical"),
    ("serialization.serialized", "mutation_contracts", "_serialized"),
    ("serialization.detached", "mutation_contracts", "detached"),
    ("serialization.digest", "mutation_contracts", "digest"),
    ("serialization.pack", "_pack_io", "canonical"),
    ("serialization.record", "research_journal_contracts", "canonical_record"),
    ("serialization.records", "research_journal_contracts", "canonical_records"),
    ("journal.detached_record", "research_journal_contracts", "detached_record"),
    ("journal.detached_records", "research_journal_contracts", "detached_records"),
    ("digest.record", "research_journal_contracts", "digest_record"),
    ("digest.records", "research_journal_contracts", "digest_records"),
    ("digest.accounting", "execution_accounting", "digest"),
    ("current.observation", "execution_session", "_observe_current"),
    ("current.qualifications", "execution_session", "_qualifications"),
    ("current.view", "execution_session", "current"),
    ("execution.reserve", "execution_session", "reserve"),
    ("execution.dispatch", "execution_session", "_external"),
    ("execution.intake", "execution_session", "intake"),
    ("execution.advance", "execution_session", "advance"),
    ("execution.native_call", "execution_session", "_native_call"),
)
LABELS = tuple(row[0] for row in _TARGETS)
MAX_DEPTH = 256
MAX_EVENTS = 10_000_000
MAX_SOURCE_BYTES = 2_097_152
_GENERATOR_FLAGS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
_MONITOR_LOCK = threading.Lock()


def _targets():
    """Resolve original owner bodies and fixed source pins before observation."""
    resolved, pins, sources = {}, [], {}
    for label, module_name, qualified in _TARGETS:
        module = importlib.import_module("evidence_wiki." + module_name)
        function = module
        for part in qualified.split("."):
            function = getattr(function, part)
        function = inspect.unwrap(function)
        code = function.__code__
        if id(code) in resolved:
            raise RuntimeError("native_profile_target_alias")
        # Keep the fixed code objects alive so identity integers cannot be
        # recycled after a module reload during the diagnostic scope.
        resolved[id(code)] = (code, label)
        source = "src/evidence_wiki/" + module_name + ".py"
        if source not in sources:
            with Path(module.__file__).open("rb") as stream:
                raw = stream.read(MAX_SOURCE_BYTES + 1)
            if len(raw) > MAX_SOURCE_BYTES:
                raise RuntimeError("native_profile_source_bound")
            sources[source] = hashlib.sha256(raw).hexdigest()
        pins.append({"label": label, "source": source, "source_sha256": sources[source],
                     "qualified_name": qualified, "code_sha256": hashlib.sha256(code.co_code).hexdigest(),
                     "generator_segments": bool(code.co_flags & _GENERATOR_FLAGS)})
    return resolved, pins


def _row():
    return {"call_events": 0, "completed_segments": 0, "inclusive_ns": 0, "exclusive_ns": 0,
            "observed_bytes": 0, "byte_results": 0, "observed_items": 0, "container_results": 0}


class NativeCostProfile:
    """Observe a single synchronous diagnostic scope; instances are single-use.

    ``report()`` is available after exit, including exceptional exit. Events and
    selected stack depth have hard ceilings. No hooks are installed in other
    threads or children; overlapping diagnostic monitoring scopes are rejected.
    Monitoring requires Python 3.14's tool cleanup API. Older runtimes may
    explicitly select the expensive ``backend="profile"`` fallback.
    The clock injection is for deterministic diagnostic verification.
    """

    def __init__(self, *, max_depth=128, max_events=MAX_EVENTS, clock=time.monotonic_ns, backend="monitoring"):
        if type(max_depth) is not int or not 1 <= max_depth <= MAX_DEPTH:
            raise ValueError("native_profile_depth_bound")
        if type(max_events) is not int or not 1 <= max_events <= MAX_EVENTS:
            raise ValueError("native_profile_event_bound")
        if not callable(clock):
            raise ValueError("native_profile_clock_invalid")
        if backend not in {"monitoring", "profile"}:
            raise ValueError("native_profile_backend_invalid")
        self._max_depth, self._max_events, self._clock = max_depth, max_events, clock
        self._backend = backend
        self._codes, self._pins, self._stack = {}, [], []
        self._rows = {label: _row() for label in LABELS}
        self._state = "new"
        self._events = self._hook_ns = self._root_ns = self._last_ns = 0
        self._dropped_depth = self._overflow_calls = self._unfinished = 0
        self._clock_regressions = self._mismatches = self._max_retained = 0
        self._truncated = self._hook_failed = False
        self._previous = None
        self._tool_id = None
        self._monitor_locked = False
        self._monitor_events = ()

    def _now(self):
        supplied = self._clock()
        if type(supplied) is not int or supplied < 0:
            raise ValueError("native_profile_clock_invalid")
        if supplied < self._last_ns:
            self._clock_regressions += 1
        self._last_ns = max(self._last_ns, supplied)
        return self._last_ns

    def __enter__(self):
        if self._state != "new":
            raise RuntimeError("native_profile_single_use")
        self._previous = sys.getprofile()
        if self._previous is not None:
            raise RuntimeError("native_profile_hook_occupied")
        if self._backend == "monitoring" and not (
                hasattr(sys, "monitoring") and hasattr(sys.monitoring, "clear_tool_id")):
            raise RuntimeError("native_profile_monitoring_unavailable")
        self._codes, self._pins = _targets()
        self._thread = threading.get_ident()
        self._start_ns = self._now()
        self._state = "active"
        try:
            if self._backend == "profile":
                sys.setprofile(self._profile)
            else:
                self._install_monitoring()
        except BaseException:
            try:
                self._stop()
            except BaseException:
                self._hook_failed = True  # Preserve the original control exception.
            finally:
                self._release_monitor_lock()
            self._state = "failed"
            raise
        return self

    def _install_monitoring(self):
        if not _MONITOR_LOCK.acquire(blocking=False):
            raise RuntimeError("native_profile_hook_occupied")
        self._monitor_locked = True
        monitor = sys.monitoring
        for candidate in (2, 3, 4, 5, 0, 1):
            if monitor.get_tool(candidate) is not None:
                continue
            try:
                monitor.use_tool_id(candidate, "native-cost-profile")
            except ValueError:
                continue  # Another tool atomically claimed the free ID.
            self._tool_id = candidate
            break
        if self._tool_id is None:
            raise RuntimeError("native_profile_tool_ids_occupied")
        events = monitor.events
        self._monitor_events = (events.PY_START, events.PY_RESUME, events.PY_RETURN,
                                events.PY_YIELD, events.PY_THROW, events.PY_UNWIND)
        for event, callback in zip(self._monitor_events, (
                self._monitor_start, self._monitor_start, self._monitor_return,
                self._monitor_return, self._monitor_throw, self._monitor_unwind), strict=True):
            monitor.register_callback(self._tool_id, event, callback)
        local = events.PY_START | events.PY_RESUME | events.PY_RETURN | events.PY_YIELD
        for code, _label in self._codes.values():
            monitor.set_local_events(self._tool_id, code, local)
        # In Python 3.12–3.14 these exception events cannot be enabled locally.
        # Their callbacks reject other codes/threads without reading any payload.
        monitor.set_events(self._tool_id, events.PY_THROW | events.PY_UNWIND)

    def _stop(self):
        with _defer_signals():
            try:
                if self._backend == "profile":
                    sys.setprofile(self._previous)
                elif self._tool_id is not None:
                    monitor, tool_id = sys.monitoring, self._tool_id
                    try:
                        monitor.set_events(tool_id, 0)
                        # Tool clearing alone leaves observable local masks on
                        # this interpreter, so clear each fixed target explicitly.
                        for code, _label in self._codes.values():
                            monitor.set_local_events(tool_id, code, 0)
                    finally:
                        try:
                            monitor.clear_tool_id(tool_id)
                        finally:
                            monitor.free_tool_id(tool_id)
                            self._tool_id = None
            finally:
                # Restore pending native signals only after ownership is gone.
                self._release_monitor_lock()

    def _release_monitor_lock(self):
        if self._monitor_locked:
            self._monitor_locked = False
            _MONITOR_LOCK.release()

    def _monitor_start(self, code, _offset):
        self._event(id(code), code, "call", None)

    def _monitor_return(self, code, _offset, result):
        self._event(id(code), code, "return", result)

    def _monitor_throw(self, code, _offset, _exception):
        self._event(id(code), code, "call", None)

    def _monitor_unwind(self, code, _offset, _exception):
        self._event(id(code), code, "return", None)

    def _profile(self, frame, event, result):
        self._event(id(frame), frame.f_code, event, result)

    def _event(self, identity, code, event, result):
        target = self._codes.get(id(code))
        if target is None or event not in {"call", "return"}:
            return
        if threading.get_ident() != self._thread:
            return
        label = target[1]
        # Ordinary diagnostic failures cannot replace native results/refusals.
        # Process-control BaseExceptions retain their native cancellation effect.
        try:
            started = self._now()
            self._events += 1
            self._segment(identity, event, result, label, started)
            ended = self._now()
            self._hook_ns += ended - started
            if self._events >= self._max_events:
                self._truncated = True
                self._abandon()
                self._stop()
        except Exception:
            self._hook_failed = True
            self._abandon()
            self._stop()
        except BaseException:
            self._hook_failed = True
            self._abandon()
            try:
                self._stop()
            except BaseException:
                self._hook_failed = True  # Preserve the original cancellation.
            raise

    def _segment(self, identity, event, result, label, now):
        if event == "call":
            self._rows[label]["call_events"] += 1
            if self._dropped_depth or len(self._stack) >= self._max_depth:
                self._dropped_depth += 1
                self._overflow_calls += 1
                return
            self._stack.append([identity, label, now, 0])
            self._max_retained = max(self._max_retained, len(self._stack))
            return
        if self._dropped_depth:
            self._dropped_depth -= 1
            return
        if not self._stack or self._stack[-1][0] != identity:
            self._mismatches += 1
            self._abandon()
            return
        _, _, started, children = self._stack.pop()
        elapsed = now - started
        row = self._rows[label]
        row["completed_segments"] += 1
        row["inclusive_ns"] += elapsed
        row["exclusive_ns"] += max(0, elapsed - children)
        if self._stack:
            self._stack[-1][3] += elapsed
        else:
            self._root_ns += elapsed
        # Exact builtin types only: no conversion, traversal or canonicalization.
        if type(result) is bytes:
            row["observed_bytes"] += len(result)
            row["byte_results"] += 1
        elif type(result) in {dict, list, tuple}:
            row["observed_items"] += len(result)
            row["container_results"] += 1

    def _abandon(self):
        self._unfinished += len(self._stack) + self._dropped_depth
        self._stack.clear()
        self._dropped_depth = 0

    def __exit__(self, *_exception):
        if threading.get_ident() != self._thread:
            raise RuntimeError("native_profile_thread_changed")
        try:
            try:
                self._stop()
            except BaseException:
                self._hook_failed = True
                if _exception[0] is None:
                    raise
        finally:
            self._release_monitor_lock()
            self._abandon()
            self._end_ns = self._last_ns
            self._exit_clock_observed = False
            try:
                # Do not mask an in-flight native deadline or cancellation by
                # calling a diagnostic clock during its exceptional unwind.
                if _exception[0] is None:
                    self._end_ns = self._now()
                    self._exit_clock_observed = True
            except Exception:
                self._hook_failed = True
            except BaseException:
                self._hook_failed = True
                raise
            finally:
                self._state = "closed"
        return False

    def report(self):
        """Return detached aggregates; timings include hook perturbation."""
        if self._state != "closed":
            raise RuntimeError("native_profile_not_closed")
        exclusive = sum(row["exclusive_ns"] for row in self._rows.values())
        window = self._end_ns - self._start_ns
        return {"schema_version": "evidence-native-cost-profile/v1", "diagnostic_only": True,
                "backend": self._backend,
                "qualification_granted": False, "scope": "current_thread_selected_code_objects",
                "instrumented_targets": [dict(row) for row in self._pins],
                "rows": [{"label": label, **self._rows[label]} for label in LABELS],
                "window_elapsed_ns": window, "selected_root_inclusive_ns": self._root_ns,
                "selected_exclusive_ns": exclusive, "unattributed_window_ns": max(0, window - self._root_ns),
                "overhead": {"observed_hook_ns": self._hook_ns, "timings_corrected": False,
                             "total_perturbation": "unknown", "scope": "selected_callback_body_clock_interval"},
                "bounds": {"max_events": self._max_events, "observed_events": self._events,
                           "event_scope": "selected_call_and_return",
                           "max_depth": self._max_depth, "max_retained_depth": self._max_retained,
                           "depth_overflow_call_events": self._overflow_calls,
                           "unfinished_segments": self._unfinished, "event_limit_reached": self._truncated},
                "reconciliation": {"exclusive_equals_selected_roots": exclusive == self._root_ns,
                                   "selected_roots_within_window": self._root_ns <= window,
                                   "clock_regressions": self._clock_regressions,
                                   "stack_mismatches": self._mismatches, "hook_failed": self._hook_failed,
                                   "exit_clock_observed": self._exit_clock_observed},
                "limitations": ["Inclusive rows overlap; sum exclusive rows for selected root time.",
                    "Exclusive time includes unselected descendants, native calls and waits.",
                    "Call events count generator resumptions; generator timing excludes suspension.",
                    "Returned bytes and container lengths are observed events, not unique data or throughput.",
                    "Overflow work remains in selected ancestor exclusive time; incomplete segments are omitted.",
                    "Other threads, children, provider billing and whole-host coverage are unobserved.",
                    "Monitoring enables normal events locally and filtered unwind/throw events globally.",
                    "The explicit profile backend dispatches every Python/C event and can be very expensive.",
                    "Pins identify on-disk source and live bytecode; they do not authenticate execution.",
                    "Exceptional exit uses the latest observed timestamp without another clock call.",
                    "Clock reads, hook dispatch and startup costs have unknown additional perturbation."]}
