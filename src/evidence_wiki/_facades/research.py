"""Typed current state, next work and explicit bounded research sessions."""

from __future__ import annotations

from ._base import Namespace, call_seam


class ResearchNamespace(Namespace):
    """Canonical lifecycle composition; current advice never grants authority."""

    @property
    def requirements(self):
        self._workspace._check_open()
        from .requirements import RequirementsNamespace
        return RequirementsNamespace(self._workspace)

    @property
    def telemetry(self):
        self._workspace._check_open()
        from .telemetry import TelemetryNamespace
        return TelemetryNamespace(self._workspace)

    def _invoke(self, seam, *args, **kwargs):
        self._workspace._check_open()
        def load(_stem):
            from .. import research_session
            return research_session
        return call_seam(load, "research_session", seam, self._root, *args, **kwargs)

    def changes(self, *, pairs, before_requirements_revision_id=None):
        """Read bounded local changes under current source and original scope."""
        self._workspace._check_open()
        def load(_stem):
            from .. import change_operations
            return change_operations
        return call_seam(load, "change_operations", "current", self._root, pairs=pairs,
            before_requirements_revision_id=before_requirements_revision_id)

    def impact(self, *, pairs=None, before_requirements_revision_id=None):
        """Explain current strict eligibility with whole-workspace invalidation."""
        self._workspace._check_open()
        def load(_stem):
            from .. import impact_operations
            return impact_operations
        return call_seam(load, "impact_operations", "current", self._root, pairs=pairs,
            before_requirements_revision_id=before_requirements_revision_id)

    def history(self, *, run_id, execution, kind=None, after_index=0, max_records=16, expected_history_basis_id=None):
        """Read retained decisions under current rights without a live run handle."""
        self._workspace._check_open()
        def load(_stem):
            from .. import history_operations
            return history_operations
        return call_seam(load, "history_operations", "current", self._root,
            run_id=run_id, execution=execution, kind=kind, after_index=after_index, max_records=max_records,
            expected_history_basis_id=expected_history_basis_id)

    def current(self, policy, *, run_id=None, execution=None):
        return self._invoke("current", policy, run_id=run_id, execution=execution)

    def next(self, policy, *, run_id=None, execution=None):
        return self._invoke("next_work", policy, run_id=run_id, execution=execution)

    def calculate(self, policy, *, run_id, execution, request_id, as_of=None):
        """Read deterministic owner values within an explicitly bound run."""
        with self.session(policy, run_id=run_id, execution=execution) as selected:
            return selected.calculate(request_id=request_id, as_of=as_of)

    def propose_calculation(self, policy, *, run_id, execution, request_id, original_id, config_ref, as_of=None):
        """Run configured formula interpretation without accepting a claim."""
        with self.session(policy, run_id=run_id, execution=execution) as selected:
            return selected.propose_calculation(request_id=request_id, original_id=original_id, config_ref=config_ref, as_of=as_of)

    def recovery_status(self, policy, *, run_id, execution, after_sequence=0, max_effects=32):
        """Observe bounded retained effects and current holders through the session."""
        with self.session(policy, run_id=run_id, execution=execution) as selected:
            return selected.recovery_status(after_sequence=after_sequence, max_effects=max_effects)

    def reconcile_recovery(self, policy, *, run_id, execution, request_id, expected_basis_id):
        """Reconcile exact retained custody using the original immutable binding."""
        with self.session(policy, run_id=run_id, execution=execution) as selected:
            return selected.reconcile_recovery(request_id=request_id, expected_basis_id=expected_basis_id)

    def command_schemas(self):
        """Return detached noninteractive command request/result shapes."""
        self._workspace._check_open()
        from ..research_command_contracts import schemas
        return schemas()

    def command(self, request, *, runtime=None):
        """Use the same public dispatch and typed terminal responses as the CLI."""
        self._workspace._check_open()
        from ..research_command_operations import invoke
        return invoke(self._workspace, request, runtime=runtime)

    def performance_schemas(self):
        """Read detached performance selection, audit and result shapes."""
        self._workspace._check_open()
        from ..performance_contracts import schemas
        return schemas()

    def qualify_performance(self, policy, *, execution, request_id, benchmark_config_ref, critic_config_ref, seed=0):
        """Qualify bounded synthetic native contracts under frozen host policy."""
        self._workspace._check_open()
        def load(_stem):
            from .. import performance_qualification
            return performance_qualification
        return call_seam(load, "performance_qualification", "run", self._root, policy, execution=execution,
            request_id=request_id, benchmark_config_ref=benchmark_config_ref, critic_config_ref=critic_config_ref, seed=seed)

    def refresh_schedule_schemas(self):
        """Return detached host scheduling shapes without registering work."""
        self._workspace._check_open()
        from ..refresh_schedule_contracts import schemas
        return schemas()

    def schedule_refresh(self, policy, *, schedule_id, previous_run_id, execution, planner_config_ref,
                         interval_seconds, expires_at, max_runs=1, max_steps=64, max_notices=64, source_updates=()):
        """Register or reopen an explicit bounded external host schedule."""
        self._workspace._check_open()
        def load(_stem):
            from .. import refresh_scheduling
            return refresh_scheduling
        return call_seam(load, "refresh_scheduling", "register", self._root, policy,
            workspace=self._workspace, schedule_id=schedule_id, previous_run_id=previous_run_id,
            execution=execution, planner_config_ref=planner_config_ref, interval_seconds=interval_seconds,
            expires_at=expires_at, max_runs=max_runs, max_steps=max_steps, max_notices=max_notices,
            source_updates=source_updates)

    def refresh_schemas(self):
        """Return the detached terminal refresh report schema."""
        self._workspace._check_open()
        from ..refresh_contracts import schemas
        return schemas()

    def refresh(self, policy, *, run_id, execution, request_id, previous_run_id,
                max_steps=64, planner_config_ref=None, source_updates=()):
        """Drive a bounded new run linked to an authenticated terminal decision."""
        with self.session(policy, run_id=run_id, execution=execution) as selected:
            return selected.refresh(request_id=request_id, previous_run_id=previous_run_id,
                max_steps=max_steps, planner_config_ref=planner_config_ref, source_updates=source_updates)

    def run_cycle(self, policy, *, run_id, execution, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None):
        """One host invocation over existing session and authenticated effect owners."""
        with self.session(policy,run_id=run_id,execution=execution) as selected:
            return selected.run_cycle(request_id=request_id,expected_basis_id=expected_basis_id,
                max_steps=max_steps,planner_config_ref=planner_config_ref)

    def session(self, policy, *, run_id, execution=None):
        session = self._invoke("session", policy, run_id=run_id, execution=execution)
        session._owner_workspace = self._workspace
        return session
