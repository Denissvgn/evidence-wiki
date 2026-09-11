#!/usr/bin/env python3
"""Opt-in, bounded arithmetic checks for inert historical simulation records.

This profile validates one fractional buy-and-hold interval. It never runs a
strategy, calls a feed, or submits an order. Domain-specific assumptions stay
inside the selected profile rather than the shared execution record schema.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from typing import Any

from _evidence_authority import EvidenceInvalid, bounded_list, exact_object, name, timestamp
from _evidence_revision import canonical_bytes, content_id
from _record_artifacts import file_binding, json_document

SCHEMA = "market-simulation/v1"
MAX_BARS = 4096
MAX_LISTINGS = 64
MAX_PERIODS = 128
METRICS = {
    "net_profit": "liquidation-equity-minus-initial-capital/v1",
    "net_return": "net-profit-divided-by-initial-capital/v1",
    "benchmark_return": "gross-close-price-return/v1",
    "max_drawdown": "maximum-peak-to-liquidation-equity-decline/v1",
}


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise EvidenceInvalid(reason)


def number(value: Any, *, minimum: str = "0", maximum: str = "1000000000000") -> Decimal:
    require(isinstance(value, str) and len(value) <= 64, "simulation_decimal_string_required")
    try:
        result = Decimal(value)
        require(result.is_finite() and len(result.as_tuple().digits) <= 32
                and abs(result.as_tuple().exponent) <= 16 and Decimal(minimum) <= result <= Decimal(maximum),
                "simulation_decimal_out_of_bounds")
        return result
    except InvalidOperation as exc:
        raise EvidenceInvalid("simulation_decimal_invalid") from exc


def reference(value: Any, files: dict[str, bytes], permitted: set[str]) -> str:
    exact_object(value, {"path", "content_hash"})
    path = value["path"]
    require(isinstance(path, str) and path in permitted and path in files
            and file_binding(files[path])["content_hash"] == value["content_hash"], "simulation_artifact_binding_mismatch")
    return path


def interval(value: Any) -> tuple:
    exact_object(value, {"start", "end"})
    start, end = timestamp(value["start"]), timestamp(value["end"])
    require(start < end, "simulation_interval_invalid")
    return start, end


def inspect(payload: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any] | None:
    """Recompute selected-profile outputs; preserve failed/incomplete observations."""
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return _inspect(payload, files)


def _inspect(payload: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any] | None:
    settings = payload["parameters"].get("market_simulation")
    if settings is None:
        return None
    require(payload["record_type"] == "observation", "simulation_observation_required")
    exact_object(settings, {"schema_version", "plan", "prices", "result"})
    require(settings["schema_version"] == SCHEMA, "simulation_schema_unsupported")
    require(payload["temporal"]["mode"] != "current", "simulation_historical_inputs_required")
    inputs = {item["artifact"]["path"]: item for item in payload["inputs"]}
    outputs = {item["path"] for item in payload["outputs"]}
    plan_path = reference(settings["plan"], files, set(inputs))
    price_path = reference(settings["prices"], files, set(inputs))
    result_path = reference(settings["result"], files, outputs)
    require(plan_path != price_path, "simulation_input_roles_conflict")
    plan = exact_object(json_document(files[plan_path]), {
        "schema_version", "strategy", "universe", "evaluation", "fold", "selected_at", "selection_cutoff",
        "training", "validation", "test", "selection_history", "benchmark", "capital", "currency", "precision",
        "costs", "fill", "adjustment", "metric_definitions", "excluded_data", "unresolved_bias", "run_configuration",
    })
    require(plan["schema_version"] == "market-simulation-plan/v1", "simulation_plan_schema_unsupported")
    require(len(inputs) == len(payload["inputs"]), "simulation_input_path_ambiguous")
    run_configuration = {key: payload[key] for key in ("seed", "tool", "model", "environment", "dependencies", "container", "patch", "verification_scope")}
    run_configuration["parameters"] = {key: value for key, value in payload["parameters"].items() if key != "market_simulation"}
    require(canonical_bytes(plan["run_configuration"]) == canonical_bytes(run_configuration), "simulation_preselected_configuration_mismatch")
    strategy_path = reference(plan["strategy"], files, set(inputs))
    universe_path = reference(plan["universe"], files, set(inputs))
    require(len({plan_path, price_path, strategy_path, universe_path}) == 4, "simulation_input_roles_conflict")
    strategy = exact_object(json_document(files[strategy_path]), {"schema_version", "allocations"})
    universe = exact_object(json_document(files[universe_path]), {"schema_version", "as_of", "basis", "complete", "members", "excluded"})
    prices = exact_object(json_document(files[price_path]), {"schema_version", "currency", "adjustment_lineage", "bars"})
    require(strategy["schema_version"] == "market-buy-hold/v1"
            and universe["schema_version"] == "market-simulation-universe/v1"
            and prices["schema_version"] == "market-simulation-prices/v1", "simulation_data_schema_unsupported")
    require(plan["metric_definitions"] == METRICS, "simulation_metric_definition_unsupported")
    evaluation = plan["evaluation"]
    require(evaluation in {"in-sample", "held-out", "walk-forward"}, "simulation_evaluation_invalid")
    require(payload["held_out_role"] == ("training" if evaluation == "in-sample" else "held-out"),
            "simulation_evaluation_role_mismatch")
    fold = exact_object(plan["fold"], {"index", "count"})
    require(type(fold["index"]) is int and type(fold["count"]) is int and 1 <= fold["index"] <= fold["count"] <= 128
            and (evaluation == "walk-forward" or fold == {"index": 1, "count": 1}), "simulation_fold_invalid")
    train, validation, test = (interval(plan[field]) for field in ("training", "validation", "test"))
    selected, cutoff = timestamp(plan["selected_at"]), timestamp(plan["selection_cutoff"])
    require(selected <= cutoff <= timestamp(payload["temporal"]["cutoff"]), "simulation_selection_cutoff_invalid")
    gaps = []
    if evaluation != "in-sample" and not (train[1] <= validation[0] and validation[1] <= selected <= cutoff <= test[0]):
        gaps.append("simulation_tuning_held_out_overlap")
    if timestamp(universe["as_of"]) != test[0] or universe["basis"] != "historical-membership" or universe["complete"] is not True:
        gaps.append("simulation_historical_universe_incomplete")
    seen_history = set()
    for trial in bounded_list(plan["selection_history"], maximum=128):
        exact_object(trial, {"trial_id", "evaluated_until", "outcome"})
        identity = name(trial["trial_id"])
        require(identity not in seen_history and trial["outcome"] in {"passed", "failed", "rejected", "inconclusive"},
                "simulation_selection_history_invalid")
        seen_history.add(identity)
        if timestamp(trial["evaluated_until"]) > selected:
            gaps.append("simulation_selection_history_after_selection")
    for field in ("excluded_data", "unresolved_bias"):
        require(all(isinstance(value, str) and 0 < len(value) <= 4096 for value in bounded_list(plan[field], maximum=128)),
                "simulation_qualification_invalid")
    members = {}
    for member in bounded_list(universe["members"], maximum=MAX_LISTINGS, minimum=1):
        exact_object(member, {"listing_id", "status"})
        listing = name(member["listing_id"])
        require(listing not in members and member["status"] in {"active", "inactive", "delisted"}, "simulation_universe_member_invalid")
        members[listing] = member["status"]
    for excluded in bounded_list(universe["excluded"], maximum=MAX_LISTINGS):
        exact_object(excluded, {"listing_id", "reason"})
        name(excluded["listing_id"])
        name(excluded["reason"])
        gaps.append("simulation_universe_exclusion")
    allocations = {}
    for item in bounded_list(strategy["allocations"], maximum=MAX_LISTINGS, minimum=1):
        exact_object(item, {"listing_id", "weight"})
        listing = name(item["listing_id"])
        require(listing in members and listing not in allocations, "simulation_allocation_listing_invalid")
        allocations[listing] = number(item["weight"], minimum="0.00000001", maximum="1")
    require(sum(allocations.values()) <= 1, "simulation_allocation_exceeds_capital")
    benchmark = name(plan["benchmark"])
    currency = name(plan["currency"])
    capital = number(plan["capital"], minimum="0.01")
    require(prices["currency"] == currency, "simulation_currency_mismatch")
    precision = exact_object(plan["precision"], {"money", "ratio"})
    require(all(value in {"1", "0.1", "0.01", "0.001", "0.0001", "0.00001", "0.000001", "0.0000001", "0.00000001"}
                for value in precision.values()), "simulation_precision_unsupported")
    costs = exact_object(plan["costs"], {"commission_bps", "spread_bps", "slippage_bps"})
    rate = sum(number(value, maximum="1000") for value in costs.values()) / Decimal(10000)
    fill = exact_object(plan["fill"], {"model", "max_volume_participation", "cash_shortfall"})
    require(fill["model"] == "fractional-close" and fill["cash_shortfall"] == "refuse", "simulation_fill_model_unsupported")
    participation = number(fill["max_volume_participation"], minimum="0.00000001", maximum="1")
    adjustment = exact_object(plan["adjustment"], {"basis", "lineage", "cash_distributions"})
    require(adjustment["basis"] == "split-dividend-adjusted" and adjustment["cash_distributions"] == "embedded",
            "simulation_adjustment_unsupported")
    require(isinstance(adjustment["lineage"], list) and len(adjustment["lineage"]) <= 128
            and all(isinstance(value, str) and name(value) for value in adjustment["lineage"]), "simulation_adjustment_lineage_invalid")
    if prices["adjustment_lineage"] != adjustment["lineage"]:
        gaps.append("simulation_adjustment_lineage_mismatch")
    bars = {}
    for bar in bounded_list(prices["bars"], minimum=2, maximum=MAX_BARS):
        exact_object(bar, {"listing_id", "start", "end", "close", "volume", "complete", "adjustment_basis"})
        listing = name(bar["listing_id"])
        start, end = interval({key: bar[key] for key in ("start", "end")})
        require(listing in members or listing == benchmark, "simulation_price_listing_outside_universe")
        require(test[0] <= start < end <= test[1], "simulation_bar_outside_test_interval")
        key = (start, end)
        values = bars.setdefault(key, {})
        require(listing not in values and len(bars) <= MAX_PERIODS, "simulation_bar_duplicate_or_bound_exceeded")
        values[listing] = (number(bar["close"], minimum="0.00000001"), number(bar["volume"]))
        if bar["complete"] is not True or end > timestamp(payload["temporal"]["cutoff"]):
            gaps.append("simulation_unfinished_bar")
        if bar["adjustment_basis"] != adjustment["basis"]:
            gaps.append("simulation_mixed_adjustment_basis")
    periods = sorted(bars)
    require(len(periods) >= 2, "simulation_price_periods_missing")
    expected_members = {*members, benchmark}
    if any(set(values) != expected_members for values in bars.values()):
        gaps.append("simulation_price_scope_incomplete")
    if any(previous[1] > following[0] for previous, following in zip(periods, periods[1:], strict=False)):
        gaps.append("simulation_overlapping_bars")
    if periods[0][0] != test[0] or periods[-1][1] != test[1] or any(
            previous[1] < following[0] for previous, following in zip(periods, periods[1:], strict=False)):
        gaps.append("simulation_price_interval_incomplete")
    needed = {*allocations, benchmark}
    computable = all(needed <= set(values) for values in bars.values())
    computation_id = content_id(SCHEMA, {"settings": {key: value for key, value in settings.items() if key != "result"},
                                        "inputs": payload["inputs"], "seed": payload["seed"],
                                        "parameters": {key: value for key, value in payload["parameters"].items() if key != "market_simulation"}})
    expected = None
    if computable:
        with localcontext() as context:
            context.prec = 80
            context.rounding = "ROUND_HALF_EVEN"
            first, last = bars[periods[0]], bars[periods[-1]]
            holdings = {listing: capital * weight / (first[listing][0] * (1 + rate)) for listing, weight in allocations.items()}
            cash = capital * (1 - sum(allocations.values()))
            if any(quantity > first[listing][1] * participation or quantity > last[listing][1] * participation
                   for listing, quantity in holdings.items()):
                gaps.append("simulation_liquidity_limit_exceeded")
            equity = [cash + sum(quantity * bars[period][listing][0] * (1 - rate) for listing, quantity in holdings.items())
                      for period in periods]
            peak, drawdown = capital, Decimal(0)
            for value in equity:
                peak = max(peak, value)
                drawdown = max(drawdown, (peak - value) / peak)

            def rounded(value: Decimal, kind: str) -> str:
                return str(value.quantize(Decimal(precision[kind])))

            expected = {"schema_version": "market-simulation-result/v1", "computation_id": computation_id,
                        "evaluation": evaluation, "fold": fold, "currency": currency,
                        "net_profit": rounded(equity[-1] - capital, "money"),
                        "net_return": rounded((equity[-1] - capital) / capital, "ratio"),
                        "benchmark_return": rounded(last[benchmark][0] / first[benchmark][0] - 1, "ratio"),
                        "max_drawdown": rounded(drawdown, "ratio"),
                        "equity_curve": [{"at": period[1].isoformat(), "liquidation_equity": rounded(value, "money")}
                                         for period, value in zip(periods, equity, strict=True)]}
    observed = json_document(files[result_path])
    matched = expected is not None and canonical_bytes(observed) == canonical_bytes(expected)
    unique_gaps = sorted(set(gaps))
    return {"schema_version": SCHEMA, "evaluation": evaluation, "fold": fold,
            "strategy_revision": inputs[strategy_path]["revision_id"], "universe_revision": inputs[universe_path]["revision_id"],
            "evidence": {"complete": not unique_gaps, "gaps": unique_gaps, "preselection_authority": "requires_accepted_input_history"},
            "calculation": {"passed": matched, "reason": "simulation_calculation_match" if matched else "simulation_calculation_mismatch",
                            "precision": precision, "expected": expected},
            "simulated_performance": observed, "eligible": matched and not unique_gaps,
            "uncertainty": {"excluded_data": plan["excluded_data"], "unresolved_bias": plan["unresolved_bias"],
                            "limitations": ["Synthetic fractional fills omit market impact, latency and queue position.",
                                            "Provider adjustment arithmetic is not reproduced.",
                                            "Historical input availability does not establish model-training cutoff."],
                            "live_execution_performance": "not_established", "model_training_cutoff": "not_established"},
            "preselection": {"mode": payload["temporal"]["mode"], "cutoff": plan["selection_cutoff"],
                             "input_revisions": sorted(item["revision_id"] for path, item in inputs.items() if path != price_path)}}
