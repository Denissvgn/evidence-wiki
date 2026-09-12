"""Generic host decisions preserve refusal and uncertain delivery outcomes."""

import contextlib
import io
from pathlib import Path

import pytest

from evidence_wiki import Workspace
from evidence_wiki.cli import main
from tests._assessment_fixture import AssessmentFixture
from tests._script_loader import load_module
from tests._simulation_fixture import SimulationFixture

HOST = load_module("host_decisions", Path(__file__).resolve().parents[1] / "examples/assessment-host/host_decisions.py")


def callbacks(envelope):
    identity = envelope["payload"]["body"]["assessment"]["assessment_id"]
    return {"check_evidence": lambda value: {"eligible": value == envelope, "assessment_id": identity},
            "approve": lambda action, identifier: True, "risk_check": lambda action: True,
            "current_state": lambda action: True, "submit": lambda action, identifier: {"accepted": identifier},
            "reconcile": lambda identifier: None}


def test_nonfinancial_approval_and_repeat_delivery_have_one_durable_decision(tmp_path):
    store = HOST.DecisionStore(tmp_path / "decisions.sqlite")
    envelope = {"payload": {"body": {"assessment": {"assessment_id": "procurement-evidence"}}}}
    calls = callbacks(envelope)
    submitted = []

    def submit(action, identifier):
        submitted.append((action, identifier))
        return {"purchase_request": identifier}

    calls["submit"] = submit
    action = {"purchase": "laboratory supplies", "amount": "75.00"}
    first = store.decide("purchase-1", action, envelope, **calls)
    assert first["status"] == "accepted"
    assert store.decide("purchase-1", action, envelope, **calls) == first
    assert len(submitted) == 1
    with pytest.raises(ValueError, match="host_decision_id_conflict"):
        store.decide("purchase-1", {**action, "amount": "750.00"}, envelope, **calls)
    calls["approve"] = lambda action, identifier: False
    assert store.decide("purchase-2", action, envelope, **calls)["reason"] == "approval_denied"
    assert len(submitted) == 1


def test_current_nonfinancial_envelope_is_checked_before_host_approval(tmp_path, monkeypatch):
    def initialize(profile):
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["init", "--profile", str(profile)]) == 0

    host = AssessmentFixture(tmp_path, monkeypatch, initialize)
    store = HOST.DecisionStore(host.host / "decisions.sqlite")
    with Workspace.open(host.root) as workspace:
        host.checkpoint = workspace.usage.transact(host.command("initialize"))["checkpoint"]
        body, files = host.temporal_source()
        host.checkpoint = workspace.usage.transact(host.command("deposit", body), artifacts=files)["checkpoint"]
        envelope = host.sign(workspace.assessments.prepare(host.request())["registration"])
        host.checkpoint = workspace.assessments.issue(envelope)["checkpoint"]
        calls = callbacks(envelope)
        calls["check_evidence"] = workspace.assessments.check
        action = {"purchase": "laboratory supplies", "amount": "75.00"}
        accepted = store.decide("purchase-1", action, envelope, **calls)
        assert accepted["status"] == "accepted"
        revocation = {"source_id": body["source_id"], "scope": "revision", "source_revision": body["source_revision"],
                      "reason": "owner-withdrawal"}
        workspace.usage.transact(host.command("revoke", revocation))
        calls["approve"] = lambda action, identifier: pytest.fail("ineligible evidence cannot reach host approval")
        assert store.decide("purchase-2", action, envelope, **calls)["reason"] == "evidence_ineligible"
        assert store.decide("purchase-1", action, envelope, **calls) == accepted


def test_ambiguous_timeout_reconciles_after_restart_without_resubmission(tmp_path):
    path = tmp_path / "decisions.sqlite"
    store = HOST.DecisionStore(path)
    envelope = {"payload": {"body": {"assessment": {"assessment_id": "simulation-evidence"}}}}
    calls = callbacks(envelope)
    submissions = []

    def submit(action, identifier):
        submissions.append(identifier)
        raise TimeoutError("provider accepted but reply was lost")

    calls["submit"] = submit
    assert store.decide("order-1", {}, envelope, **calls)["status"] == "uncertain"
    restarted = HOST.DecisionStore(path)
    assert restarted.decide("order-1", {}, envelope, **calls)["status"] == "uncertain"
    calls["reconcile"] = lambda identifier: {"provider_receipt": identifier}
    assert restarted.decide("order-1", {}, envelope, **calls)["status"] == "accepted"
    assert submissions == ["order-1"]


def test_valid_losing_simulation_can_be_declined_by_host_risk_policy(tmp_path, monkeypatch):
    host = SimulationFixture(tmp_path, monkeypatch)
    body, files = host.simulation(mode="historical-available")
    report, qualification = host.assessment(body, files)
    assert qualification["eligible"]
    calculation = report["records"][0]["market_simulation"]
    assert calculation["calculation"]["passed"]
    assert calculation["simulated_performance"]["net_profit"] == "-68.81"
    # This example policy consumes independently validated simulation output;
    # historical simulation is not an authenticated current-action assessment.
    allowed = float(calculation["simulated_performance"]["net_profit"]) >= -50
    envelope = {"payload": {"body": {"assessment": {"assessment_id": "host-simulation-review"}}}}
    calls = callbacks(envelope)
    calls["risk_check"] = lambda action: allowed

    def forbidden(action, identifier):
        pytest.fail("risk refusal must precede any submission")

    calls["submit"] = forbidden
    store = HOST.DecisionStore(tmp_path / "decisions.sqlite")
    result = store.decide("simulation-review-1", {}, envelope, **calls)
    assert result["status"] == "declined" and result["reason"] == "risk_denied"
    host.workspace.close()
