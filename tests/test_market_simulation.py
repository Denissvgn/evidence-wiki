"""Synthetic arithmetic, historical selection and authority remain separate."""

from __future__ import annotations

import copy
import json
from decimal import Inexact, localcontext
from unittest.mock import patch

import pytest

from evidence_wiki import verify_snapshot
from evidence_wiki.errors import SourceError
from tests._execution_fixture import binding, canonical, identifier
from tests._historical_fixture import late_signature
from tests._simulation_fixture import SimulationFixture


@pytest.fixture
def host(tmp_path, monkeypatch):
    return SimulationFixture(tmp_path, monkeypatch)


@pytest.mark.parametrize("mode", ["historical-audit", "historical-available"])
def test_losing_portfolio_has_independent_calculation_and_historical_inputs(host, mode):
    body, files = host.simulation(mode=mode)
    with patch("socket.create_connection", side_effect=AssertionError("offline")), patch("subprocess.run", side_effect=AssertionError("inert")):
        report, assessment = host.assessment(body, files)
        assert assessment["eligible"], assessment
        profile = report["records"][0]["market_simulation"]
        assert profile["evidence"]["complete"] and profile["calculation"]["passed"]
        assert profile["simulated_performance"]["net_profit"] == "-68.81"
        assert profile["simulated_performance"]["net_return"] == "-0.068812"
        assert profile["simulated_performance"]["benchmark_return"] == "0.030000"
        assert profile["uncertainty"]["live_execution_performance"] == "not_established"
        assert assessment["historical_inputs"]["qualified_source_cutoffs"] == 7
        data, preparation, _command, _result = host.export(host.selection(body))
        assert json.loads(data)["schema_version"] == "evidence-snapshot/v4"
        assert preparation["manifest"]["execution_profiles"] == ["market-simulation/v1"]
        assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]
        assert preparation["manifest"]["examples"][0]["market_simulation"] == profile
        assert host.workspace.snapshots.check(data)["current_use"] == "authorized"
    assert host.simulation_data["universe.json"]["members"][1]["status"] == "delisted"
    assert host.simulation_data["plan.json"]["selection_history"][0]["outcome"] == "rejected"


@pytest.mark.parametrize("mutation,reason", [
    (lambda data: data["prices.json"]["bars"][0].update(complete=False), "simulation_unfinished_bar"),
    (lambda data: data["universe.json"].update(complete=False), "simulation_historical_universe_incomplete"),
    (lambda data: data["plan.json"]["validation"].update(end="2026-09-10T03:00:00Z"), "simulation_tuning_held_out_overlap"),
    (lambda data: data["prices.json"]["bars"][0].update(adjustment_basis="raw"), "simulation_mixed_adjustment_basis"),
    (lambda data: data["prices.json"]["bars"][0].update(volume="1"), "simulation_liquidity_limit_exceeded"),
    (lambda data: data["prices.json"].update(adjustment_lineage=[]), "simulation_adjustment_lineage_mismatch"),
    (lambda data: data["universe.json"]["excluded"].append({"listing_id": "missing:venue", "reason": "survivors-only"}), "simulation_universe_exclusion"),
])
def test_signed_pass_does_not_erase_declared_evidence_gaps(host, mutation, reason):
    body, files = host.simulation(edit=mutation)
    report, assessment = host.assessment(body, files)
    assert report["valid"] and not assessment["eligible"]
    profile = report["records"][0]["market_simulation"]
    assert reason in profile["evidence"]["gaps"] and not profile["eligible"]
    before = (host.host / "evidence-state.json").read_bytes()
    with pytest.raises(SourceError):
        host.workspace.snapshots.prepare(host.selection(body))
    assert (host.host / "evidence-state.json").read_bytes() == before


@pytest.mark.parametrize("option,reason", [("extra_input", "temporal_future_publication"), ("late_plan", "temporal_not_host_observed_at_cutoff")])
def test_run_cutoff_cannot_promote_inputs_unavailable_when_strategy_was_selected(host, option, reason):
    body, files = host.simulation(**{option: True})
    report, assessment = host.assessment(body, files)
    assert report["valid"] and not assessment["eligible"] and assessment["reason"] == reason, assessment
    assert not any(receipt["positive_eligible"] for receipt in assessment["receipts"])


@pytest.mark.parametrize("outcome", ["passed", "failed"])
def test_wrong_calculation_is_never_positive_and_honest_failure_is_exportable(host, outcome):
    body, files = host.simulation(outcome=outcome, alter_result=lambda result: result.update(net_return="0.950000"))
    report, assessment = host.assessment(body, files)
    assert report["valid"] and not assessment["eligible"]
    assert not report["records"][0]["market_simulation"]["calculation"]["passed"]
    if outcome == "passed":
        with pytest.raises(SourceError):
            host.workspace.snapshots.prepare(host.selection(body))
    else:
        data, preparation, _command, _result = host.export(host.selection(body, negatives=True))
        assert preparation["manifest"]["examples"][0]["label"] == "negative"
        assert verify_snapshot(data, trust_policy_bytes=host.policy_path.read_bytes())["valid"]


@pytest.mark.parametrize("field", ["input", "cost", "parameter", "output"])
def test_changed_computation_inputs_do_not_reuse_previous_result(host, field):
    host.simulation()
    payload, files = copy.deepcopy(host.simulation_payload), dict(host.simulation_files)
    settings = payload["parameters"]["market_simulation"]
    path = "prices.json" if field == "input" else "plan.json" if field == "cost" else "result.json"
    document = json.loads(files[path])
    if field == "input":
        document["bars"][0]["close"] = "120"
    elif field == "cost":
        document["costs"]["commission_bps"] = "90"
    elif field == "output":
        document["net_profit"] = "100.00"
    else:
        payload["seed"] = 42
    files[path] = canonical(document)
    ref = {"path": path, "content_hash": binding(files[path])["content_hash"]}
    settings["prices" if field == "input" else "plan" if field == "cost" else "result"] = ref
    for item in payload["inputs"]:
        if item["artifact"]["path"] == path:
            item["artifact"] = ref
    simulator = host.workspace._script("_market_simulation")
    if field == "parameter":
        with pytest.raises(ValueError, match="simulation_preselected_configuration_mismatch"):
            simulator.inspect(payload, files)
    else:
        assert not simulator.inspect(payload, files)["calculation"]["passed"]


@pytest.mark.parametrize("evaluation,fold", [("in-sample", {"index": 1, "count": 1}), ("walk-forward", {"index": 2, "count": 3})])
def test_evaluation_roles_and_precision_remain_explicit(host, evaluation, fold):
    body, files = host.simulation(edit=lambda data: data["plan.json"].update(evaluation=evaluation, fold=fold))
    with localcontext() as context:
        context.prec = 3
        context.rounding = "ROUND_DOWN"
        context.traps[Inexact] = True
        report, assessment = host.assessment(body, files)
    assert assessment["eligible"], assessment
    profile = report["records"][0]["market_simulation"]
    assert profile["evaluation"] == evaluation and profile["fold"] == fold
    assert report["records"][0]["held_out_role"] == ("training" if evaluation == "in-sample" else "held-out")


@pytest.mark.parametrize("change", ["control", "missing-profile", "unknown-profile", "downgrade", "performance", "evidence", "precision"])
def test_independent_verifier_recomputes_optional_profile_even_with_host_registration(host, change):
    body, _files = host.simulation(mode="historical-available")
    data, _, _, _ = host.export(host.selection(body))
    bundle = json.loads(data)
    manifest = bundle["manifest"]
    profile = manifest["examples"][0]["market_simulation"]
    if change == "missing-profile":
        manifest["execution_profiles"] = []
    elif change == "unknown-profile":
        manifest["execution_profiles"] = ["market-simulation/v999"]
    elif change == "downgrade":
        bundle["schema_version"] = "evidence-snapshot/v3"
        manifest.update(schema_version="evidence-snapshot-manifest/v3", contract="evidence-snapshot-contract/v3")
        manifest["exporter"]["version"] = manifest["contract"]
        del manifest["execution_profiles"], manifest["examples"][0]["market_simulation"]
    elif change == "performance":
        profile["simulated_performance"]["net_profit"] = "100.00"
    elif change == "evidence":
        profile["evidence"]["gaps"] = ["invented-gap"]
    elif change == "precision":
        profile["calculation"]["precision"]["money"] = "1"
    bundle["snapshot_id"] = identifier(manifest["schema_version"], manifest)
    bundle["registration"]["payload"]["body"]["node_id"] = bundle["snapshot_id"]
    bundle["registration"] = late_signature(bundle["registration"]["payload"], "owner", "usage")
    result = verify_snapshot(canonical(bundle), trust_policy_bytes=host.policy_path.read_bytes())
    assert result["valid"] is (change == "control"), result
