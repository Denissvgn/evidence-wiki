"""Public journal/custody parity and namespace lifetime with real owners."""

from __future__ import annotations

import pytest

from evidence_wiki import Workspace
from evidence_wiki.errors import EvidenceWikiError
from tests.test_research_session import GRANTS as SESSION_GRANTS
from tests.test_research_session import host, policy
from tests.test_telemetry import event
from tests.test_telemetry import session_root as _session_root

GRANTS = SESSION_GRANTS | {
    "telemetry.record", "telemetry.report", "telemetry.project",
    "run_controller.run_telemetry_record", "run_controller.run_telemetry_events",
    "mutation_operations.prepare_request", "mutation_operations.submit", "mutation_operations.inspect",
}


@pytest.fixture
def root(tmp_path):
    return _session_root.__wrapped__(tmp_path)


def test_public_namespace_uses_real_canonical_record_and_exact_coordinator_replay(root, tmp_path):
    with host(root, tmp_path, grants=GRANTS).activate(), Workspace.open(root) as workspace:
        session = workspace.research.session(policy(), run_id="run")
        basis = session.current()["basis_id"]
        session.advance(request_id="stop", expected_basis_id=basis)
        namespace = workspace.research.telemetry
        observation = event()
        original = namespace.record(observation, run_id="run", agent_id="caller")
        assert namespace.record(observation, run_id="run", agent_id="caller") == original
        request = workspace.mutations.prepare(request_id="record", operation="telemetry.record",
            arguments={"run_id": "run", "agent_id": "caller", "event": observation})
        receipt = workspace.mutations.submit(request)
        assert receipt["status"] == "committed" and receipt["result"] == original
        assert workspace.mutations.submit(request) == receipt
        report = namespace.report(run_id="run")
        assert report["metrics"][0]["known_subtotal"] == "0.125"
        assert report["metrics"][0]["total"] is None
        projection = namespace.project(report)
        assert projection["measurement_authentication"] == "not_reverified"
        assert projection["report_is_declared"]
        assert not receipt["authorized"] and not receipt["evidence_accepted"]
        workspace.close()
        with pytest.raises(EvidenceWikiError) as error:
            namespace.report(run_id="run")
        assert error.value.error_code == "WORKSPACE_UNREADABLE"


def test_public_property_and_lazy_loader_respect_closed_lifetime_and_process_boundary(root, tmp_path, monkeypatch):
    with host(root, tmp_path, grants=GRANTS).activate():
        workspace = Workspace.open(root)
        namespace = workspace.research.telemetry
        import evidence_wiki.telemetry as module

        def exited(*args, **kwargs):
            raise SystemExit(0)

        monkeypatch.setattr(module, "report", exited)
        with pytest.raises(EvidenceWikiError) as error:
            namespace.report(run_id="run")
        assert error.value.error_code == "LIBRARY_PROCESS_EXIT"
        workspace.close()
        with pytest.raises(EvidenceWikiError):
            _ = workspace.research.telemetry
