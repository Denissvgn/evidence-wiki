"""Synthetic simulation data with an independent rational-arithmetic oracle."""

from __future__ import annotations

import copy
from decimal import Decimal, localcontext
from fractions import Fraction

from tests._execution_fixture import binding, canonical, closure, example, identifier
from tests._historical_fixture import HistoricalFixture, late_signature


def documents():
    strategy = {"schema_version": "market-buy-hold/v1", "allocations": [{"listing_id": "issuer-a:venue-a", "weight": "0.5"},
                                                                     {"listing_id": "issuer-b:venue-a", "weight": "0.5"}]}
    universe = {"schema_version": "market-simulation-universe/v1", "as_of": "2026-09-10T02:00:00Z",
                "basis": "historical-membership", "complete": True,
                "members": [{"listing_id": "issuer-a:venue-a", "status": "active"},
                            {"listing_id": "issuer-b:venue-a", "status": "delisted"}], "excluded": []}
    plan = {"schema_version": "market-simulation-plan/v1", "strategy": None, "universe": None,
            "evaluation": "held-out", "fold": {"index": 1, "count": 1}, "selected_at": "2026-09-10T00:30:00Z",
            "selection_cutoff": "2026-09-10T01:00:00Z",
            "training": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"},
            "validation": {"start": "2026-09-02T00:00:00Z", "end": "2026-09-03T00:00:00Z"},
            "test": {"start": "2026-09-10T02:00:00Z", "end": "2026-09-10T04:00:00Z"},
            "selection_history": [{"trial_id": "discarded-fit", "evaluated_until": "2026-09-03T00:00:00Z", "outcome": "rejected"}],
            "benchmark": "benchmark:venue-a", "capital": "1000", "currency": "USD",
            "precision": {"money": "0.01", "ratio": "0.000001"},
            "costs": {"commission_bps": "50", "spread_bps": "30", "slippage_bps": "20"},
            "fill": {"model": "fractional-close", "max_volume_participation": "0.01", "cash_shortfall": "refuse"},
            "adjustment": {"basis": "split-dividend-adjusted", "lineage": ["split-a", "dividend-b"], "cash_distributions": "embedded"},
            "metric_definitions": {"net_profit": "liquidation-equity-minus-initial-capital/v1",
                "net_return": "net-profit-divided-by-initial-capital/v1", "benchmark_return": "gross-close-price-return/v1",
                "max_drawdown": "maximum-peak-to-liquidation-equity-decline/v1"}, "excluded_data": [], "unresolved_bias": []}
    prices = {"schema_version": "market-simulation-prices/v1", "currency": "USD", "adjustment_lineage": ["split-a", "dividend-b"], "bars": []}
    for hour, values in ((2, ("100", "50", "100")), (3, ("110", "40", "103"))):
        for listing, value in zip(("issuer-a:venue-a", "issuer-b:venue-a", "benchmark:venue-a"), values, strict=True):
            prices["bars"].append({"listing_id": listing, "start": f"2026-09-10T0{hour}:00:00Z", "end": f"2026-09-10T0{hour+1}:00:00Z",
                                   "close": value, "volume": "10000", "complete": True, "adjustment_basis": "split-dividend-adjusted"})
    return {"strategy.json": strategy, "universe.json": universe, "plan.json": plan, "prices.json": prices}


def independently_recalculate(data, payload):
    """Rational cash accounting is independent of the production Decimal path."""
    plan, strategy, prices = (data[name] for name in ("plan.json", "strategy.json", "prices.json"))
    capital = Fraction(plan["capital"])
    drag = sum(Fraction(value) for value in plan["costs"].values()) / 10000
    timeline = sorted({bar["end"] for bar in prices["bars"]})
    closes = {(bar["end"], bar["listing_id"]): Fraction(bar["close"]) for bar in prices["bars"]}
    cash, positions = capital, {}
    for allocation in strategy["allocations"]:
        listing, budget = allocation["listing_id"], capital * Fraction(allocation["weight"])
        cash -= budget
        positions[listing] = budget / closes[timeline[0], listing] / (1 + drag)
    amounts = []
    for instant in timeline:
        liquidation = cash
        for listing, units in positions.items():
            sale = units * closes[instant, listing]
            liquidation += sale - sale * drag
        amounts.append(liquidation)
    drawdowns = [(max([capital, *amounts[:index + 1]]) - value) / max([capital, *amounts[:index + 1]])
                 for index, value in enumerate(amounts)]

    def rounded(value, kind):
        with localcontext() as context:
            context.prec = 80
            context.rounding = "ROUND_HALF_EVEN"
            return str((Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal(plan["precision"][kind])))

    settings = payload["parameters"]["market_simulation"]
    computation_id = identifier("market-simulation/v1", {"settings": {key: value for key, value in settings.items() if key != "result"},
        "inputs": payload["inputs"], "seed": payload["seed"],
        "parameters": {key: value for key, value in payload["parameters"].items() if key != "market_simulation"}})
    return {"schema_version": "market-simulation-result/v1", "computation_id": computation_id,
            "evaluation": plan["evaluation"], "fold": plan["fold"], "currency": plan["currency"],
            "net_profit": rounded(amounts[-1] - capital, "money"), "net_return": rounded((amounts[-1] - capital) / capital, "ratio"),
            "benchmark_return": rounded(closes[timeline[-1], plan["benchmark"]] / closes[timeline[0], plan["benchmark"]] - 1, "ratio"),
            "max_drawdown": rounded(max(drawdowns), "ratio"),
            "equity_curve": [{"at": instant.replace("Z", "+00:00"), "liquidation_equity": rounded(value, "money")}
                             for instant, value in zip(timeline, amounts, strict=True)]}


class SimulationFixture(HistoricalFixture):
    def simulation(self, *, edit=None, alter_result=None, extra_input=False, late_plan=False, mode="historical-audit", outcome="passed"):
        data = documents()
        if edit is not None:
            edit(data)
        files, record = example(history=False)
        del files["inputs.txt"], files["result.txt"], files["execution-record.json"]
        payload = copy.deepcopy(record["records"][0]["payload"])
        payload["verification_scope"].update(suite_revision="fraction-calculator/1", checks=["portfolio-liquidation"])
        data["plan.json"]["run_configuration"] = {key: copy.deepcopy(payload[key]) for key in (
            "seed", "tool", "model", "environment", "dependencies", "container", "patch", "verification_scope")}
        data["plan.json"]["run_configuration"]["parameters"] = {}
        inputs, parents = [], []

        def ref(path):
            return {"path": path, "content_hash": binding(files[path])["content_hash"]}

        def capture(path, instant):
            files[path] = canonical(data[path])
            source, original = self.captured("simulation:" + path, evidence={path: files[path]}, published=instant, available=instant,
                                            temporal={"measurement": None, "record_path": None, "provenance_path": None})
            self.availability(source, original)
            inputs.append({"source_id": source["source_id"], "revision_id": source["source_revision"], "artifact": ref(path)})
            parents.append(source["source_revision"])

        for path in ("strategy.json", "universe.json"):
            capture(path, "2026-09-10T00:20:00Z")
        data["plan.json"].update(strategy=ref("strategy.json"), universe=ref("universe.json"))
        if late_plan:
            self.set_time("2026-09-10T04:30:00Z")
        capture("plan.json", "2026-09-10T00:30:00Z")
        self.set_time("2026-09-10T04:30:00Z")
        capture("prices.json", "2026-09-10T04:00:00Z")
        if extra_input:
            data["filing.json"] = {"published_at": "2026-09-10T03:00:00Z", "content": "Future statement used in strategy selection."}
            capture("filing.json", "2026-09-10T03:00:00Z")
        self.set_time("2026-09-10T05:00:00Z")
        payload.update(episode_id="synthetic-simulation", task_id="portfolio-calculation", run_id="measured-run", problem_id="bounded-buy-hold",
            input_group_id="historical-universe", inputs=inputs, outcome=outcome,
            held_out_role="training" if data["plan.json"]["evaluation"] == "in-sample" else "held-out",
            parameters={"market_simulation": {"schema_version": "market-simulation/v1", "plan": ref("plan.json"), "prices": ref("prices.json"), "result": None}},
            workspace={"scope": "declared-input-artifacts", "content_hash": closure({item["artifact"]["path"]: files[item["artifact"]["path"]] for item in inputs})},
            temporal={"mode": mode, "cutoff": "2026-09-10T04:30:00Z", "limitations": []},
            started_at="2026-09-10T04:50:00Z", finished_at="2026-09-10T04:51:00Z",
            units={"net_profit": "USD", "net_return": "ratio", "benchmark_return": "ratio", "max_drawdown": "ratio"})
        result = independently_recalculate(data, payload)
        if alter_result is not None:
            alter_result(result)
        files["result.json"] = canonical(result)
        payload["parameters"]["market_simulation"]["result"] = ref("result.json")
        payload["outputs"] = [ref("result.json")]
        record_id = identifier("evidence-execution-record/v1", payload)
        receipt = copy.deepcopy(record["receipts"][0]["payload"])
        receipt.update(target_record_id=record_id, episode_id=payload["episode_id"], task_id=payload["task_id"], run_id=payload["run_id"],
            scope=payload["verification_scope"], started_at="2026-09-10T04:52:00Z", finished_at="2026-09-10T04:53:00Z", outcome=outcome,
            assertions=[{"check_id": "portfolio-liquidation", "outcome": outcome, "expected": independently_recalculate(data, payload),
                         "actual": result, "comparison": "exact-rational-reference/1"}],
            counts={key: int(key == outcome) for key in ("passed", "failed", "skipped", "inconclusive")})
        roles = {member["path"]: member["role"] for member in record["artifacts"]}
        roles.update(dict.fromkeys((item["artifact"]["path"] for item in inputs), "input"))
        roles["result.json"] = "output"
        record.update(source_id="execution:simulation", records=[late_signature(payload, "runner", "generator")],
                      receipts=[late_signature(receipt, "evaluator", "evaluator")], selected_record_id=record_id,
                      selected_receipt_id=identifier("evidence-verification-receipt/v1", receipt),
                      artifacts=[{"path": path, **binding(raw), "role": roles[path]} for path, raw in sorted(files.items())])
        files["execution-record.json"] = canonical(record)
        body, originals = self.captured("execution:simulation", evidence=files, parents=parents,
            published="2026-09-10T05:00:00Z", available="2026-09-10T05:00:00Z",
            temporal={"measurement": None, "expires_at": None, "record_path": None, "provenance_path": None})
        self.availability(body, originals)
        self.simulation_data, self.simulation_payload, self.simulation_files = data, payload, files
        return body, originals
