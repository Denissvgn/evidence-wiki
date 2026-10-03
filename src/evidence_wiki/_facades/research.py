"""Typed current state, next work and explicit bounded research sessions."""

from __future__ import annotations

from ._base import Namespace, call_seam


class ResearchNamespace(Namespace):
    """Canonical lifecycle composition; current advice never grants authority."""

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

    def session(self, policy, *, run_id, execution=None):
        session = self._invoke("session", policy, run_id=run_id, execution=execution)
        session._owner_workspace = self._workspace
        return session
