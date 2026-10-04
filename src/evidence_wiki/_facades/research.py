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

    def run_cycle(self, policy, *, run_id, execution, request_id, expected_basis_id=None, max_steps=64, planner_config_ref=None):
        """One host invocation over existing session and authenticated effect owners."""
        with self.session(policy,run_id=run_id,execution=execution) as selected:
            return selected.run_cycle(request_id=request_id,expected_basis_id=expected_basis_id,
                max_steps=max_steps,planner_config_ref=planner_config_ref)

    def session(self, policy, *, run_id, execution=None):
        session = self._invoke("session", policy, run_id=run_id, execution=execution)
        session._owner_workspace = self._workspace
        return session
