"""Domain-neutral authenticated research handoff and bounded reevaluation."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class AssessmentsNamespace(Namespace):
    def prepare(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("evidence_assessments", "run_operation", self._root, operation="prepare", request=request)

    def issue(self, command: dict[str, Any]) -> dict[str, Any]:
        return self._call("evidence_assessments", "run_operation", self._root, operation="issue", request=command)

    def check(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return self._call("evidence_assessments", "run_operation", self._root, operation="check", request=envelope)

    def plan_refresh(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("evidence_assessments", "run_operation", self._root, operation="plan-refresh", request=request)

    def apply_refresh(self, command: dict[str, Any]) -> dict[str, Any]:
        return self._call("evidence_assessments", "run_operation", self._root, operation="apply-refresh", request=command)
