"""Explicit host polling and finite dispatch over stored native refresh policy."""

from datetime import datetime, timedelta, timezone

from . import execution_session as native
from . import refresh_schedule_queue as queue
from .errors import EvidenceWikiError
from .history_operations import _subject
from .host_context import checked_operation, current_context
from .mutation_contracts import canonical, detached, digest, validate_request_id
from .mutation_operations import _generation, _host_selection
from .pack_discovery import owner
from .refresh_schedule_contracts import CURSOR, MAX_COMMANDS, MAX_EVENTS, NOTICE, PLAN, STATUS, checked, utc
from .refresh_schedule_store import ScheduleStore
from .refresh_session import _terminal, _updates
from .research_execution import ResearchExecution
from .research_session_contracts import refuse
from .run_policy import RunPolicy

STATE = "research-host-refresh-state/v1"
REFRESH_GRANTS = ("research_session.refresh", "refresh_owner.apply", "research_session.run_cycle",
    "research_session.brief", "research_session.resolution", "brief_operations.current", "resolution_operations.current")


def now():
    return datetime.now(timezone.utc).isoformat()


def register(root, policy, *, workspace, schedule_id, previous_run_id, execution, planner_config_ref,
             interval_seconds, expires_at, max_runs=1, max_steps=64, max_notices=64, source_updates=()):
    if type(policy) is not RunPolicy or type(execution) is not ResearchExecution:
        refuse("SCHEDULE_CONFIG_INVALID")
    schedule_id = validate_request_id(schedule_id)
    plan = checked(PLAN, {"schema_version": PLAN, "previous_run_id": previous_run_id,
        "planner_config_ref": planner_config_ref, "interval_seconds": interval_seconds, "expires_at": expires_at,
        "max_runs": max_runs, "max_steps": max_steps, "max_notices": max_notices, "source_updates": _updates(source_updates)})
    handle = HostRefreshSchedule(workspace, policy, execution, schedule_id, plan)
    with handle._boundary("register", write=True):
        observed = handle._snapshot()
        if observed["basis_id"] is None:
            refuse("SCHEDULE_NATIVE_SCOPE_UNAVAILABLE")
        subject, records = _subject(root, previous_run_id, execution)
        terminal = _terminal(records)
        closure = next((row["data"]["closure"] for row in reversed(records) if row["kind"] == "execution_close"), None)
        document = owner("run_controller")._session_state(root, previous_run_id)
        if (terminal is None or closure is None or document["state"]["current"] not in owner("run_controller").TERMINAL_STATES
                or closure["terminal_decision_ref"] != digest(terminal["data"]["step"]["decision"])):
            refuse("REFRESH_TERMINAL_PARENT_REQUIRED")
        if subject.binding["original_ids"] != observed["original_ids"]:
            refuse("ORIGINALS_CHANGED")
        accounting = native.accounting.projection(execution.ledger(subject, records))
        from .refresh_session import selected
        inherited = selected(records)
        ancestor_unknown = bool(inherited and inherited["parent_usage_unknown"])
        clock = now()
        if not handle._new_store().exists() and not utc(clock) < utc(expires_at) <= utc(clock)+timedelta(days=30):
            refuse("SCHEDULE_EXPIRY_INVALID")
        with handle._new_store().locked(write=True) as store:
            state = store.read()
            if state is not None:
                handle._verify(state)
                return handle
            if not store.new:
                refuse("SCHEDULE_STATE_UNAVAILABLE")
            clock = now()
            if not utc(clock) < utc(expires_at) <= utc(clock)+timedelta(days=30):
                refuse("SCHEDULE_EXPIRY_INVALID")
            state = {"schema_version": STATE, "binding": handle._binding, "binding_id": digest(handle._binding),
                "previous_run_id": previous_run_id, "last_basis_id": observed["basis_id"], "last_observed_at": clock,
                "next_due_at": (utc(clock)+timedelta(seconds=interval_seconds)).isoformat(),
                "notices": {}, "source_sequences": {}, "commands": {}, "events": [], "jobs": [], "pending": None,
                "change_generation": 0, "settled_generation": 0, "allocated_runs": 0, "fulfilled_updates": [],
                "usage": {"seconds": 0, "model_calls": 0, "tool_calls": 0, "tokens": 0, "cost_limit": "0"},
                "usage_unknown": (accounting["usage_unknown"] or ancestor_unknown),
                "terminal_reason": "parent_usage_unknown" if (accounting["usage_unknown"] or ancestor_unknown) else None, "authentication": None}
            handle._save(state, store)
    return handle


class HostRefreshSchedule:
    """A parent-lifetime host handle; constructing it never starts a daemon."""

    def __init__(self, workspace, policy, execution, schedule_id, plan):
        self._workspace, self._policy, self._execution = workspace, policy, execution
        self._root, self._context = workspace.root, current_context()
        self._plan, self._schedule_id = plan, schedule_id
        if self._context is None or self._context.mutation_control_directory is None:
            refuse("HOST_CONTEXT_REQUIRED")
        self._store = ScheduleStore(self._root, self._context.mutation_control_directory, schedule_id)
        self._binding = {"schedule_id": schedule_id, "plan": plan, "policy": policy.to_dict(),
            "host_context_sha256": _host_selection(), "owner_generation": _generation(),
            "workspace_binding": self._store_binding(), **execution.identity()}

    def _new_store(self):
        return ScheduleStore(self._root, self._context.mutation_control_directory, self._schedule_id)

    def _store_binding(self):
        store = self._store
        from ._pack_io import identity
        return {"root": str(store.root), "identity": identity(store.root),
            "control": str(store.control), "control_identity": identity(store.control)}

    def _boundary(self, operation, *, write):
        self._workspace._check_open()
        if current_context() is not self._context or _host_selection() != self._binding["host_context_sha256"]:
            refuse("SCHEDULE_BINDING_CHANGED")
        if any(self._binding.get(key) != value for key, value in self._execution.identity().items()):
            refuse("SCHEDULE_BINDING_CHANGED")
        if _generation() != self._binding["owner_generation"] or self._store_binding() != self._binding["workspace_binding"]:
            refuse("SCHEDULE_BINDING_CHANGED")
        return checked_operation("refresh_scheduling", operation, (self._root,), {})

    def _snapshot(self):
        try:
            return self._native_snapshot()
        except EvidenceWikiError:
            return {"basis_id": None, "source_ids": set(), "authority": False, "eligible": False, "original_ids": None}

    def _native_snapshot(self):
        # This probe never starts a run or substitutes for the current brief.
        with self._workspace.research.session(self._policy,
                run_id=native.child_id(self._schedule_id, "scheduler-observation"), execution=self._execution) as probe:
            from .execution_planning import configured
            configured(probe, self._plan["planner_config_ref"])
            with native._current_facts(probe) as facts:
                view = facts.view
                return {"basis_id": view["basis_id"], "source_ids": {row["source_id"] for row in facts.packets},
                    "authority": bool(view["policy_compatible"] and view["authority"]["terminal_grants_available"]
                        and view["qualification"]["state"] == "qualified"),
                    "eligible": all(row["accepted"] for row in view["accounting"]["original_outcomes"]),
                    "original_ids": view["accounting"]["original_ids"]}

    def _verify(self, state):
        self._execution._verify(state)
        if (state.get("schema_version") != STATE or state.get("binding") != self._binding
                or state.get("binding_id") != digest(self._binding)):
            refuse("SCHEDULE_BINDING_CHANGED")
        for field, maximum in (("events", MAX_EVENTS), ("commands", MAX_COMMANDS), ("notices", self._plan["max_notices"]),
                ("jobs", self._plan["max_runs"])):
            if len(state[field]) > maximum:
                refuse("SCHEDULE_STATE_INVALID")
        pending = state["pending"]
        reserved = int(pending is not None and pending["state"] == "dispatching")
        if (state["allocated_runs"] != len(state["jobs"])+reserved or state["allocated_runs"] > self._plan["max_runs"]
                or state["settled_generation"] > state["change_generation"]):
            refuse("SCHEDULE_STATE_INVALID")
        for index, row in enumerate(state["events"], 1):
            if row["sequence"] != index or row["event_id"] != digest({key: value for key, value in row.items() if key != "event_id"}):
                refuse("SCHEDULE_STATE_INVALID")

    def _load(self, store):
        state = store.read()
        if state is None:
            refuse("SCHEDULE_STATE_UNAVAILABLE")
        self._verify(state)
        return state

    def _save(self, state, store):
        signed = self._execution.sign({**state, "authentication": None})
        self._verify(signed)
        if len(canonical(signed)) > 1_048_576:
            refuse("LIMIT")
        store.write(signed)

    def _page(self, state, observation, cursor, max_events, *, historical=False):
        if type(max_events) is not int or not 1 <= max_events <= 32:
            refuse("LIMIT")
        position = 0
        if cursor is not None:
            cursor = checked(CURSOR, cursor)
            self._execution._verify(cursor)
            position = cursor["sequence"]
            if (cursor["binding_id"] != state["binding_id"] or position > len(state["events"])
                    or cursor["event_ref"] != (state["events"][position-1]["event_id"] if position else None)):
                refuse("SCHEDULE_CURSOR_INVALID")
        events = state["events"][position:position+max_events]
        position += len(events)
        continuation = self._execution.sign({"schema_version": CURSOR, "binding_id": state["binding_id"],
            "sequence": position, "event_ref": state["events"][position-1]["event_id"] if position else None, "authentication": None})
        pending = state["pending"]
        active = pending is not None and pending["state"] == "dispatching"
        terminal = state["terminal_reason"] is not None and not active
        status = "running" if active else "exhausted" if terminal else "blocked" if not observation["authority"] else "ready" if pending else "waiting"
        return checked(STATUS, {"schema_version": STATUS, "schedule_id": self._schedule_id, "binding_id": state["binding_id"],
            "status": status, "terminal": terminal, "reason": state["terminal_reason"] or ("current_native_authority" if observation["authority"] else "current_authority_unavailable"),
            "previous_run_id": state["previous_run_id"], "next_due_at": state["next_due_at"],
            "pending": None if pending is None else {key: pending[key] for key in ("job_id", "run_id", "state", "reason", "notice_count")},
            "allocated_runs": state["allocated_runs"], "max_runs": self._plan["max_runs"], "notices_retained": len(state["notices"]),
            "usage_unknown": state["usage_unknown"], "recorded_usage": {"known_totals": state["usage"], "complete": not state["usage_unknown"], "scope": "scheduled_refresh_dispatches"}, "current_authority": observation["authority"],
            "historical": historical, "events": events, "cursor": continuation, "truncated": position < len(state["events"]),
            "current_eligibility": "not_evaluated", "authorized": False, "evidence_accepted": False, "daemon_created": False})

    def current(self, *, cursor=None, max_events=16):
        with self._boundary("current", write=False), self._new_store().locked(write=False) as store:
            state = self._load(store)
            observation = self._snapshot()
            return self._page(state, observation, cursor, max_events)

    def poll(self, *, request_id, notices=(), cursor=None, max_events=16):
        return self._tick(request_id, notices, cursor, max_events, dispatch=False)

    def advance(self, *, request_id, notices=(), cursor=None, max_events=16):
        return self._tick(request_id, notices, cursor, max_events, dispatch=True)

    def _tick(self, request_id, notices, cursor, max_events, *, dispatch):
        request_id = validate_request_id(request_id)
        if type(notices) not in {tuple, list} or len(notices) > 32:
            refuse("SCHEDULE_NOTICE_INVALID")
        notices = [checked(NOTICE, row) for row in notices]
        with self._boundary("advance" if dispatch else "poll", write=True):
            # A dispatch lease is independent of the workspace gate and held only
            # by an advancing host. Polling can observe/coalesce queued work.
            if dispatch:
                with self._new_store().dispatch_lease():
                    return self._process(request_id, notices, cursor, max_events, dispatch=True)
            return self._process(request_id, notices, cursor, max_events, dispatch=False)

    def _process(self, request_id, notices, cursor, max_events, *, dispatch):
        recovering = False
        with self._new_store().locked(write=True) as store:
            state = self._load(store)
            observation = self._snapshot()
            self._page(state, observation, cursor, max_events)
            request, replay = queue.command(state, request_id, {"notices": notices, "dispatch": dispatch})
            if not replay and request is not None:
                queue.ingest(state, notices, observation["source_ids"])
            queue.reconcile_due(state, observation, now())
            job = state["pending"]
            recoverable = dispatch and job is not None and job["state"] == "dispatching" and observation["authority"]
            if (request is None and not recoverable or request is not None and request["complete"]
                    or not dispatch or job is None or not observation["authority"]):
                if request is not None and job is None:
                    request["complete"] = True
                self._save(state, store)
                return self._page(state, observation, cursor, max_events, historical=replay)
            if job["state"] == "dispatching":
                recovering = True
                if request is not None:
                    request["job_id"] = job["job_id"]
            else:
                for grant in REFRESH_GRANTS:
                    self._context.require_grant(grant)
                queue.reserve(state, job, request)
            job = detached(job)
            self._save(state, store)
        report = self._dispatch(job, recovering=recovering)
        with self._new_store().locked(write=True) as store:
            state = self._load(store)
            queue.settle(state, job, report, now())
            observation = self._snapshot()
            self._save(state, store)
            return self._page(state, observation, cursor, max_events, historical=replay)

    def _dispatch(self, job, *, recovering):
        if recovering:
            path = owner("run_controller").run_state_path(self._root, job["run_id"])
            if not path.exists():
                return None
        try:
            return self._workspace.research.refresh(self._policy, run_id=job["run_id"],
                previous_run_id=job["previous_run_id"], execution=self._execution, request_id=job["request_id"],
                planner_config_ref=self._plan["planner_config_ref"], max_steps=self._plan["max_steps"], source_updates=job["source_updates"])
        except EvidenceWikiError:
            return None
