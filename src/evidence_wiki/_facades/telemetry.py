"""Explicit journal observations and host-selected telemetry delivery."""

from __future__ import annotations

from types import SimpleNamespace

from ._base import Namespace, call_seam


class TelemetryNamespace(Namespace):
    """Inert measurement data; host callbacks do not approve research."""

    def _invoke(self, stem, seam, **kwargs):
        self._workspace._check_open()

        def load(_stem):
            if stem == "telemetry":
                from .. import telemetry
                return telemetry
            from .. import telemetry_delivery
            return telemetry_delivery

        return call_seam(load, stem, seam, self._root, **kwargs)

    def record(self, event, *, run_id, agent_id):
        return self._invoke("telemetry", "record", run_id=run_id, agent_id=agent_id, event=event)

    def report(self, *, run_id, verifier=None, execution_session=None):
        return self._invoke("telemetry", "report", run_id=run_id, verifier=verifier, execution_session=execution_session)

    def project(self, report):
        self._workspace._check_open()

        def load(_stem):
            from ..telemetry import project
            return SimpleNamespace(project=lambda _root, value: project(value))

        return call_seam(load, "telemetry", "project", self._root, report)

    def deliver(self, projection, *, run_id, delivery_id, sink, max_attempts=3):
        return self._invoke("telemetry_delivery", "deliver", run_id=run_id,
            delivery_id=delivery_id, projection=projection, sink=sink, max_attempts=max_attempts)

    def inspect(self, delivery_id):
        return self._invoke("telemetry_delivery", "inspect", delivery_id=delivery_id)

    def retry(self, delivery_id, *, sink):
        return self._invoke("telemetry_delivery", "retry", delivery_id=delivery_id, sink=sink)
