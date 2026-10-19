"""Authenticated producer provenance is consumed before plain JSON retention."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from evidence_wiki._native_owner_execution import execution_policy
from tests.test_integration_improvement import NOW, fixture
from tools import _integration_improvement as gate
from tools import measure_integration_costs as observer
from tools import run_native_owner_feasibility as runner
from tools._integration_costs import METRICS, freeze_cost_plan
from tools._release_decision import canonical, digest


@pytest.mark.parametrize("action", ["decide", "replay"])
@pytest.mark.parametrize("outcome,exit_code", [("continue", 0), ("revise", 2)])
def test_actual_v2_receipt_is_detached_only_after_producer_proof(tmp_path, monkeypatch, action, outcome, exit_code):
    plan, _, pins, readers = fixture()
    policy = execution_policy()
    plan["scope"].update(execution_policy=policy, execution_policy_sha256=digest(policy),
                         native_cleanup_recipe="owned-popen-groups/v2", native_run_seconds=policy["native_run_seconds"])
    frozen = datetime.fromisoformat(plan["frozen_at"].replace("Z", "+00:00"))
    plan["expires_at"] = (frozen + timedelta(seconds=policy["study_seconds"])).isoformat()
    for slot in plan["slots"]:
        slot["elapsed_cap_seconds"] = policy["slot_seconds"]
    report = gate.decide(plan, digest(plan), pins, digest(pins), readers, now=NOW)
    report["outcome"] = outcome
    report["report_sha256"] = digest({key: value for key, value in report.items() if key != "report_sha256"})
    script = tmp_path / "fixture_report.py"
    script.write_text("import sys\nsys.stdout.buffer.write(" + repr(canonical(report)) + ")\nsys.exit(" + str(exit_code) + ")\n")
    script.chmod(0o600)
    receipts, work = tmp_path / "receipts", tmp_path / "work"
    receipts.mkdir(mode=0o700)
    work.mkdir(mode=0o700)
    (tmp_path / "decisions").mkdir(mode=0o700)
    key = tmp_path / "observer.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    now = datetime.now(timezone.utc)
    cost_plan = freeze_cost_plan(scope_id="retention", binding={"integration_plan_sha256": digest(plan)}, sources=[{
        "source_id": "host", "provenance": "host_observed", "kind": "automated", "origin": "Fixture stdout",
        "collection_started_at": (now - timedelta(seconds=1)).isoformat(),
        "collection_ended_at": (now + timedelta(minutes=2)).isoformat(), "coverage": "partial", "authentication_ref": None}],
        attempts=[{"attempt_id": action, "phase": "operation", "allocation": "repeated", "source_id": "host",
                   "required_metrics": list(METRICS)}])
    config = {"cost_plan": cost_plan, "receipt_directory": str(receipts), "proof_work_root": str(work), "key_path": str(key)}
    originals = {"originals": "fixture"}
    args = SimpleNamespace(action=action, expected_originals=digest(originals), expected_evidence=digest(pins),
                           expected_decision_raw=runner.raw_pin(canonical(report)))
    decision_path = tmp_path / "decisions/decision.json"
    if action == "replay":
        runner.publish_new(decision_path, report)
    monkeypatch.setattr(runner, "load_study", lambda args: (tmp_path, {"recipe": {"decision_seconds": 5}},
                                                           plan, {"primary": config}))
    actual_document = runner.document
    monkeypatch.setattr(runner, "document", lambda path: originals if Path(path).name == "originals.json" else
                        pins if Path(path).name == "evidence-pins.json" else actual_document(path))
    monkeypatch.setattr(runner, "evidence_pins", lambda *args: pins)

    def invocation(root, name, argv, *args):
        # Substitute only the coordinator's command; observation/authentication remain real.
        argv[:] = [str(runner.ROOT / ".venv/bin/python"), str(script)]
        return "invocation-pin"

    monkeypatch.setattr(runner, "_invocation", invocation)
    authenticated, events = [], []
    actual_reader = runner.read_command_receipt

    def read(*args, **kwargs):
        result = actual_reader(*args, **kwargs)
        assert type(result) is observer._AuthenticatedCommandReceipt
        authenticated.append(result)
        events.append("authenticate")
        return result

    monkeypatch.setattr(runner, "read_command_receipt", read)
    actual_proof = observer.producer_cleanup_proof

    def proof(verified, **kwargs):
        assert verified is authenticated[0]
        result = actual_proof(verified, **kwargs)
        for copied in (dict(verified), json.loads(canonical(verified))):
            with pytest.raises(ValueError, match="cost_authenticated_producer_receipt_required"):
                actual_proof(copied, **kwargs)
        events.append("producer_proof")
        return result

    monkeypatch.setattr(observer, "producer_cleanup_proof", proof)

    def cleanup(path, **kwargs):
        assert events == ["authenticate", "producer_proof"]
        assert kwargs["producer_cleanup"]["command_receipt_sha256"] == authenticated[0]["receipt_sha256"]
        events.append("cleanup")
        return {"confirmed": True}

    monkeypatch.setattr(runner, "verify_child_registry", cleanup)
    actual_publish = runner.publish_new

    def publish(path, value):
        if Path(path).name == action + "-retention.json":
            assert events == ["authenticate", "producer_proof", "cleanup"]
            assert type(value["observation"]) is dict
            assert value["observation"] is not authenticated[0]
            assert canonical(value) == canonical(dict(value, observation=authenticated[0]))
            events.append("publish_retention")
        actual_publish(path, value)

    monkeypatch.setattr(runner, "publish_new", publish)
    result = runner.execute(args)
    raw = (tmp_path / "decisions" / (action + "-retention.json")).read_bytes()
    published = json.loads(raw)
    assert raw == canonical(published)
    assert result == published | {"retention_sha256": digest(published)}
    assert result["verified"] is True and result["failure_classes"] == []
    assert result["output_hidden"] is (action == "replay")
    assert result["observation"]["receipt"]["schema_version"] == "integration-cost-receipt/v2"
    assert decision_path.read_bytes() == canonical(report)
    assert type(authenticated[0]) is observer._AuthenticatedCommandReceipt
    assert events == ["authenticate", "producer_proof", "cleanup", "publish_retention"]
