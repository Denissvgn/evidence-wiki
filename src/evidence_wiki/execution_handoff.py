"""Native source custody in the actual protected stdin delivery thread."""

import hashlib
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from .agent_execution import ExecutionError
from .mutation_contracts import digest
from .pack_discovery import owner
from .protected_handoff import NativeInputHandoff
from .repair_surface import require_current_dependencies


def refused(reason):
    raise ExecutionError("protected_handoff_" + reason)


class NativeHandoffLease:
    """An installed parent operation, never a caller permit or worker ACK."""

    def __init__(self, session, attempt, *, wait_seconds, started_monotonic):
        from .research_session import ResearchSession
        if type(session) is not ResearchSession or type(session.execution.profile.input_handoff) is not NativeInputHandoff:
            refused("declaration_required")
        self.session, self.attempt = session, attempt
        self._view = None
        self._config = None
        if type(wait_seconds) not in (int, float) or not 0 < wait_seconds <= 86400:
            refused("wait_invalid")
        if type(started_monotonic) not in (int, float) or not 0 < started_monotonic <= time.monotonic():
            refused("wait_invalid")
        self._deadline = started_monotonic + min(wait_seconds, session.execution.profile.input_handoff.delivery_seconds)
        self._clock = datetime.now(timezone.utc)
        self._thread = None
        self._capture = None
        self._wall_deadline = None
        self._worker_sha256 = None
        self._completed_sha256 = None
        self.delivered_sha256 = None

    @contextmanager
    def custody(self, payload):
        from . import execution_session as native
        session, attempt = self.session, self.attempt
        self.delivered_sha256 = self._completed_sha256 = None
        with session.context.activate():
            self._thread = threading.get_ident()
            self._time_check()
            session._check("advance", write=True)
            from .research_session import _load
            if native._document(session)["state"]["current"] in _load(session.root, "run_controller").TERMINAL_STATES:
                refused("run_terminal")
            rows = [r["data"] for r in native.retained(session) if r["kind"] == "execution_attempt"
                and r["data"]["attempt"]["attempt_id"] == attempt.attempt_id]
            if len(rows) != 1 or rows[0]["attempt"] != attempt.document():
                refused("owned_attempt_changed")
            row = rows[0]
            if row.get("retry_purpose", row.get("purpose")) != "repair" or "repair_parent_inventory" not in row:
                refused("route_unsupported")
            self._worker_sha256 = hashlib.sha256(payload).hexdigest()
            if payload != native.canonical(attempt.worker_document()):
                refused("worker_input_changed")
            config = owner("_strict_evidence").configuration(session.root)
            usage = owner("_evidence_usage")
            if not usage.configured(config):
                refused("native_usage_required")
            now = datetime.now(timezone.utc)
            wall_deadline = min(datetime.fromisoformat(attempt.job["deadline_utc"]), native.deadline(session))
            self._wall_deadline = wall_deadline
            remaining = (wall_deadline - now).total_seconds()
            if remaining <= 0:
                refused("deadline_expired")
            self._deadline = min(self._deadline, time.monotonic() + remaining)
            with usage.current_view(session.root, config) as view:
                self._view, self._config = view, config
                try:
                    self._capture = owner("_evidence_revision").capture_workspace(session.root)
                    self.checkpoint()
                    current_view = native.current(session)
                    if (current_view["basis_id"] != attempt.job["basis_id"]
                            or current_view.get("policy_compatible") is not True
                            or current_view.get("qualification", {}).get("state") != "qualified"):
                        refused("current_basis_or_policy_changed")
                    packets, _bindings = native._sources(session)
                    current = {p["source_id"]: p for p in packets}
                    retained = row.get("repair_parent_sources", attempt.input_document.get("sources", []))
                    if not retained or not attempt.input_document.get("sources"):
                        refused("dependency_closure_missing")
                    for expected in retained:
                        actual = current.get(expected["source_id"])
                        if (actual is None or actual["record_sha256"] != expected["record_sha256"]
                                or actual.get("source_binding") != expected.get("source_binding")):
                            refused("dependency_not_current_permitted")
                        matches = [revision for revision, stored in view.state.revisions.items()
                            if stored["source_id"] == expected["source_id"] and stored["descriptor"]["normalized_path"] is not None
                            and "sha256:" + hashlib.sha256(stored["files"][stored["descriptor"]["normalized_path"]]).hexdigest() == expected["record_sha256"]]
                        if len(matches) != 1:
                            refused("dependency_revision_ambiguous")
                        decision = view.check(matches[0], uses=["retrieval"], purpose="research", consumer="evidence-wiki")
                        if decision.get("eligible") is not True or decision.get("complete") is not True:
                            refused("dependency_not_current_permitted")
                    if "repair_parent_inventory" in row:
                        require_current_dependencies(row["repair_parent_inventory"], packets, attempt.input_document.get("computation"))
                    prepared = owner("strict_evidence").prepare_review(session.root, row["target_claim_id"])
                    result = prepared["result"]
                    inventory = {"schema_version": row["repair_parent_inventory"]["schema_version"],
                        "questions": result["questions"], "claims": [item["claim"] for item in result["claims"]]}
                    if inventory != row["repair_parent_inventory"]:
                        refused("material_changed")
                    self.checkpoint()
                    yield self
                    self.checkpoint()
                finally:
                    self._view = None
                    self._thread = None
            # The native manager's final trust/expiry/selection validation must
            # succeed before a parent delivery fact becomes available.
            self._time_check()
            session._check("advance", write=True)
        self.delivered_sha256 = self._completed_sha256

    def _time_check(self):
        if time.monotonic() >= self._deadline:
            refused("deadline_expired")
        now = datetime.now(timezone.utc)
        if now < self._clock:
            refused("clock_regressed")
        if self._wall_deadline is not None and now >= self._wall_deadline:
            refused("deadline_expired")
        self._clock = now

    def checkpoint(self):
        self._time_check()
        if self._view is None or self._thread != threading.get_ident():
            refused("custody_thread_changed")
        self.session._check("advance", write=True)
        self._view.revalidate(self.session.root, self._config)
        if self._capture is not None and owner("_evidence_revision").capture_workspace(self.session.root).revision_id != self._capture.revision_id:
            refused("material_changed")
        self._time_check()

    def complete(self, payload):
        self.checkpoint()
        if hashlib.sha256(payload).hexdigest() != self._worker_sha256:
            refused("worker_input_changed")
        self._completed_sha256 = self._worker_sha256
        return {"attempt_id": self.attempt.attempt_id, "job_sha256": self.attempt.job_sha256,
            "input_sha256": self.attempt.input_sha256, "worker_sha256": self._completed_sha256,
            "binding_ref": digest(self.session.binding), "boundary": "native_stdin_complete", "accepted": False}
