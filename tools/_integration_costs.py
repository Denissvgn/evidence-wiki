"""Bounded pure projections of declared integration-cost receipt inventories.

Inputs are inert data. A declaration of host provenance or complete coverage
does not authenticate a producer, independently enumerate host work, or prove
human effort, adoption, business value, or savings.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from tools._journey_cases import canonical, digest, require

PHASES = ("integration", "setup", "repair", "operation", "review", "qualification", "evidence_update")
METRICS = ("elapsed_seconds", "model_wait_seconds", "model_calls", "tool_calls", "tokens", "cost")
PROVENANCES = ("host_observed", "imported", "estimated", "synthetic")
STATUSES = ("succeeded", "failed", "abandoned", "abstained", "interrupted", "unknown")
ALLOCATIONS = ("one_time", "repeated")
PLAN_VERSION = "integration-cost-plan/v1"
REPORT_VERSION = "integration-cost-report/v1"
MAX_BYTES = 4_194_304
MAX_ATTEMPTS = 1024
MAX_EVENTS = 2048
MAX_SOURCES = 128
_COUNTS = frozenset({"model_calls", "tool_calls", "tokens"})
_UTC = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)"


def _copy(value):
    """Bound the tree before encoding, including cyclic or non-JSON callers."""
    pending = [(value, 0, frozenset())]
    nodes, size = 0, 0
    while pending:
        item, depth, ancestors = pending.pop()
        nodes += 1
        require(nodes <= 100_000 and depth <= 16, "cost_input_bound")
        if type(item) in {dict, list}:
            require(id(item) not in ancestors, "cost_input_cycle")
            parents = ancestors | {id(item)}
            if type(item) is dict:
                require(len(item) <= 128 and all(type(key) is str for key in item), "cost_input_object")
                children = [*item.keys(), *item.values()]
            else:
                require(len(item) <= MAX_EVENTS, "cost_input_bound")
                children = item
            pending.extend((child, depth + 1, parents) for child in children)
        elif type(item) is str:
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                raise ValueError("cost_input_unicode") from None
            require(size <= MAX_BYTES, "cost_input_bound")
        elif type(item) is int:
            require(item.bit_length() <= 128, "cost_input_bound")
        else:
            require(item is None or type(item) is bool, "cost_input_type")
    raw = canonical(value)
    require(len(raw) <= MAX_BYTES, "cost_input_bound")
    return json.loads(raw)


def _object(value, fields):
    require(type(value) is dict and set(value) == set(fields), "cost_shape_invalid")


def _text(value, maximum=4096):
    require(type(value) is str and 0 < len(value.encode("utf-8")) <= maximum, "cost_text_invalid")


def _id(value):
    _text(value, 128)
    require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", value) is not None, "cost_id_invalid")


def _sha(value):
    require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "cost_digest_invalid")


def _count(value, *, minimum=0):
    require(type(value) is int and minimum <= value <= 1_000_000_000_000, "cost_count_invalid")


def _decimal(value):
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,12})?", value) is not None,
            "cost_decimal_invalid")


def _time(value):
    require(type(value) is str and re.fullmatch(_UTC, value) is not None, "cost_time_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("cost_time_invalid") from None


def _window(start, end):
    require((start is None) == (end is None), "cost_interval_incomplete")
    if start is not None:
        require(_time(start) <= _time(end), "cost_interval_order")


def _enum(value, choices):
    require(type(value) is str and value in choices, "cost_enum_invalid")


def _source(value):
    _object(value, {"source_id", "provenance", "kind", "origin", "collection_started_at", "collection_ended_at",
                    "coverage", "authentication_ref"})
    _id(value["source_id"])
    _enum(value["provenance"], PROVENANCES)
    _enum(value["kind"], ("automated", "human", "organization"))
    _enum(value["coverage"], ("complete", "partial", "unknown"))
    _text(value["origin"])
    if value["authentication_ref"] is not None:
        _text(value["authentication_ref"])
    _window(value["collection_started_at"], value["collection_ended_at"])
    if value["coverage"] == "complete":
        require(value["collection_started_at"] is not None
                and _time(value["collection_started_at"]) < _time(value["collection_ended_at"]),
                "cost_complete_source_window")


def freeze_cost_plan(*, scope_id, binding, sources, attempts, repeat_use_count=None):
    """Freeze an explicit inventory; its digest confers no host authority."""
    value = _copy({"schema_version": PLAN_VERSION, "scope_id": scope_id, "binding": binding, "sources": sources,
                   "attempts": attempts, "repeat_use_count": repeat_use_count})
    value["plan_sha256"] = digest(value)
    return validate_cost_plan(value)


def validate_cost_plan(value):
    """Decode a closed detached plan and check its exact canonical identity."""
    value = _copy(value)
    _object(value, {"schema_version", "scope_id", "binding", "sources", "attempts", "repeat_use_count", "plan_sha256"})
    require(value["schema_version"] == PLAN_VERSION, "cost_plan_version")
    _id(value["scope_id"])
    require(type(value["binding"]) is dict and 1 <= len(value["binding"]) <= 32, "cost_binding_invalid")
    _sha(value["plan_sha256"])
    require(value["plan_sha256"] == digest({key: item for key, item in value.items() if key != "plan_sha256"}),
            "cost_plan_digest_conflict")
    if value["repeat_use_count"] is not None:
        _count(value["repeat_use_count"], minimum=1)
    sources = value["sources"]
    require(type(sources) is list and 1 <= len(sources) <= MAX_SOURCES, "cost_source_inventory")
    for source in sources:
        _source(source)
    source_map = {source["source_id"]: source for source in sources}
    require(len(source_map) == len(sources), "cost_source_duplicate")
    attempts = value["attempts"]
    require(type(attempts) is list and 1 <= len(attempts) <= MAX_ATTEMPTS, "cost_attempt_inventory")
    for attempt in attempts:
        _object(attempt, {"attempt_id", "phase", "allocation", "source_id", "required_metrics"})
        _id(attempt["attempt_id"])
        _id(attempt["source_id"])
        _enum(attempt["phase"], PHASES)
        _enum(attempt["allocation"], ALLOCATIONS)
        require(attempt["source_id"] in source_map and source_map[attempt["source_id"]]["kind"] == "automated",
                "cost_attempt_source")
        metrics = attempt["required_metrics"]
        require(type(metrics) is list and 1 <= len(metrics) <= len(METRICS), "cost_required_metrics")
        for metric in metrics:
            _enum(metric, METRICS)
        require(len(set(metrics)) == len(metrics), "cost_required_metrics")
    require(len({attempt["attempt_id"] for attempt in attempts}) == len(attempts), "cost_attempt_duplicate")
    return value


def _money(value):
    _object(value, {"amount", "currency", "pricing_ref"})
    _decimal(value["amount"])
    require(type(value["currency"]) is str and re.fullmatch(r"[A-Z]{3}", value["currency"]) is not None,
            "cost_currency_invalid")
    _text(value["pricing_ref"])


def _event(plan, value, attempt_map, source_map):
    _object(value, {"event_id", "attempt_id", "plan_sha256", "source_id", "receipt_sha256", "status", "started_at",
                    "finished_at", "metrics"})
    for key in ("event_id", "attempt_id", "source_id"):
        _id(value[key])
    _sha(value["receipt_sha256"])
    require(value["plan_sha256"] == plan["plan_sha256"], "cost_event_plan_binding")
    attempt = attempt_map.get(value["attempt_id"])
    require(attempt is not None and value["source_id"] == attempt["source_id"], "cost_event_source_binding")
    _enum(value["status"], STATUSES)
    _window(value["started_at"], value["finished_at"])
    source = source_map[value["source_id"]]
    if value["started_at"] is not None:
        require(source["collection_started_at"] is not None
                and _time(source["collection_started_at"]) <= _time(value["started_at"])
                and _time(value["finished_at"]) <= _time(source["collection_ended_at"]), "cost_event_outside_source")
    _object(value["metrics"], METRICS)
    for key, metric in value["metrics"].items():
        if metric is not None:
            if key == "cost":
                _money(metric)
            elif key in _COUNTS:
                _count(metric)
            else:
                _decimal(metric)
    usage = value["metrics"]
    if usage["model_wait_seconds"] is not None and usage["elapsed_seconds"] is not None:
        require(_compare(usage["model_wait_seconds"], usage["elapsed_seconds"]) <= 0, "cost_wait_exceeds_elapsed")
    return value


def validate_cost_event(plan, value):
    """Validate one cumulative final receipt against its frozen attempt binding."""
    plan = validate_cost_plan(plan)
    return _event(plan, _copy(value), {row["attempt_id"]: row for row in plan["attempts"]},
                  {row["source_id"]: row for row in plan["sources"]})


def _integer(value):
    whole, _, fraction = value.partition(".")
    return int(whole + fraction), len(fraction)


def _format(value, scale):
    if scale == 0:
        return str(value)
    digits = str(value).zfill(scale + 1)
    fraction = digits[-scale:].rstrip("0")
    return digits[:-scale] + ("." + fraction if fraction else "")


def _sum(values):
    numbers = [_integer(value) for value in values]
    scale = max((places for _, places in numbers), default=0)
    return _format(sum(value * 10 ** (scale - places) for value, places in numbers), scale)


def _compare(left, right):
    a, sa = _integer(left)
    b, sb = _integer(right)
    scale = max(sa, sb)
    return (a * 10 ** (scale - sa) > b * 10 ** (scale - sb)) - (a * 10 ** (scale - sa) < b * 10 ** (scale - sb))


def _summary(attempts, event_map, source_map, *, coverage_complete=True):
    """Sum all outcomes once; partial known subtotals do not become totals."""
    metrics, cost_groups, unknown_cost = {}, {}, []
    scope_complete = coverage_complete and all(
        source_map[row["source_id"]]["coverage"] == "complete"
        and event_map.get(row["attempt_id"], {}).get("status", "unknown") != "unknown" for row in attempts)
    for key in METRICS[:-1]:
        known, unknown = [], []
        for attempt in attempts:
            event = event_map.get(attempt["attempt_id"])
            value = None if event is None else event["metrics"][key]
            if value is None:
                unknown.append(attempt["attempt_id"])
            else:
                known.append(str(value))
        subtotal = _sum(known) if known else None
        if subtotal is not None and key in _COUNTS:
            subtotal = int(subtotal)
        metrics[key] = {"total": subtotal if attempts and not unknown and scope_complete else None,
                        "known_subtotal": subtotal, "unknown_attempt_ids": unknown}
    for attempt in attempts:
        event = event_map.get(attempt["attempt_id"])
        value = None if event is None else event["metrics"]["cost"]
        if value is None:
            unknown_cost.append(attempt["attempt_id"])
        else:
            key = (value["currency"], value["pricing_ref"])
            cost_groups.setdefault(key, []).append(value["amount"])
    subtotals = [{"currency": currency, "pricing_ref": pricing_ref, "amount": _sum(values)}
                 for (currency, pricing_ref), values in sorted(cost_groups.items())]
    return {"attempt_count": len(attempts),
            "status_counts": {status: sum(event_map.get(row["attempt_id"], {}).get("status", "missing") == status
                                          for row in attempts) for status in (*STATUSES, "missing")},
            "metrics": metrics, "cost": {"total": subtotals[0] if len(subtotals) == 1 and not unknown_cost
                                           and scope_complete else None,
                                          "subtotals": subtotals, "unknown_attempt_ids": unknown_cost}}


def _seconds(delta):
    return _format((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds, 6)


def _intervals(attempts, event_map, source_map):
    ranges, unknown = [], []
    for attempt in attempts:
        event = event_map.get(attempt["attempt_id"])
        if event is None or event["started_at"] is None:
            unknown.append(attempt["attempt_id"])
        else:
            ranges.append((_time(event["started_at"]), _time(event["finished_at"])))
    ranges.sort()
    merged = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return {"observed_interval_count": len(ranges), "unknown_attempt_ids": unknown,
            "span_seconds": _seconds(max(end for _, end in ranges) - ranges[0][0]) if ranges else None,
            "union_seconds": _sum([_seconds(end - start) for start, end in merged]) if merged else None,
            "complete": bool(attempts) and not unknown and all(
                source_map[row["source_id"]]["coverage"] == "complete"
                and event_map[row["attempt_id"]]["status"] != "unknown" for row in attempts)}


def _external(plan, values, source_map, used_receipts):
    require(type(values) is list and len(values) <= MAX_EVENTS, "cost_external_bound")
    retained = {}
    for value in values:
        _object(value, {"observation_id", "source_id", "receipt_sha256", "metric", "value", "denominator", "coverage"})
        _id(value["observation_id"])
        _id(value["source_id"])
        _sha(value["receipt_sha256"])
        _enum(value["metric"], ("human_interventions", "human_seconds", "adoption_count", "business_outcome"))
        _enum(value["coverage"], ("complete", "partial", "unknown"))
        source = source_map.get(value["source_id"])
        expected = "human" if value["metric"].startswith("human_") else "organization"
        require(source is not None and source["kind"] == expected, "cost_external_source")
        if value["value"] is not None:
            if value["metric"] in {"human_interventions", "adoption_count"}:
                _count(value["value"])
            else:
                _decimal(value["value"])
        if value["denominator"] is not None:
            _count(value["denominator"], minimum=1)
        existing = retained.get(value["observation_id"])
        if existing is not None:
            require(existing == value, "cost_external_conflict")
            continue
        require(value["receipt_sha256"] not in used_receipts, "cost_receipt_reused")
        used_receipts.add(value["receipt_sha256"])
        retained[value["observation_id"]] = value
    return [{**retained[key], "provenance": source_map[retained[key]["source_id"]]["provenance"],
             "telemetry": "declared", "authentication": "unproved"}
            for key in sorted(retained)]


def _external_status(observations, source_map):
    statuses = {"telemetry": "declared", "authentication": "unproved"}
    for category, metrics in (("human", {"human_interventions", "human_seconds"}), ("adoption", {"adoption_count"}),
                              ("business", {"business_outcome"})):
        rows = [row for row in observations if row["metric"] in metrics
                and row["provenance"] in {"host_observed", "imported"}]
        usable = [row for row in rows if row["value"] is not None]
        complete = usable and len(usable) == len(rows) and all(row["coverage"] == "complete"
                   and source_map[row["source_id"]]["coverage"] == "complete"
                   and (category == "human" or row["denominator"] is not None) for row in usable)
        statuses[category] = "declared_observed" if complete else "partial" if usable else "unobserved"
    return statuses


def project_cost_report(plan, events, *, external_observations=None):
    """Project declared receipts without pricing inference or evidence promotion.

    Elapsed totals add attempt durations, which can overlap. Interval union and
    span are separate partial observations. Every report remains unauthenticated
    by this reducer, including a declaration with no missing receipt metrics.
    """
    plan = validate_cost_plan(plan)
    events = _copy(events)
    require(type(events) is list and len(events) <= MAX_EVENTS, "cost_event_bound")
    sources = {row["source_id"]: row for row in plan["sources"]}
    attempts = {row["attempt_id"]: row for row in plan["attempts"]}
    event_map, event_ids, used_receipts = {}, {}, set()
    for event in events:
        _event(plan, event, attempts, sources)
        existing = event_map.get(event["attempt_id"])
        if existing is not None:
            require(existing == event, "cost_attempt_receipt_conflict")
            continue
        require(event["event_id"] not in event_ids, "cost_event_id_conflict")
        require(event["receipt_sha256"] not in used_receipts, "cost_receipt_reused")
        used_receipts.add(event["receipt_sha256"])
        event_ids[event["event_id"]] = event
        event_map[event["attempt_id"]] = event
    missing = sorted(set(attempts) - set(event_map))
    missing_metrics = [{"attempt_id": key, "metric": metric} for key in sorted(attempts)
                       for metric in METRICS if metric in attempts[key]["required_metrics"]
                       and (key not in event_map or event_map[key]["metrics"][metric] is None)]
    incomplete_sources = sorted({row["source_id"] for row in attempts.values()
                                 if sources[row["source_id"]]["coverage"] != "complete"})
    unknown_status = sorted(key for key, row in event_map.items() if row["status"] == "unknown")
    unobserved = [phase for phase in PHASES if not any(row["phase"] == phase for row in attempts.values())]
    declared_complete = not (missing or missing_metrics or incomplete_sources or unknown_status)
    all_metrics_known = all(key in event_map and all(event_map[key]["metrics"][metric] is not None for metric in METRICS)
                            for key in attempts)
    complete = declared_complete and not unobserved and all_metrics_known
    ordered_attempts = [attempts[key] for key in sorted(attempts)]
    measured = [row for row in ordered_attempts if sources[row["source_id"]]["provenance"] == "host_observed"]
    by_phase = {phase: {"declared": _summary([row for row in ordered_attempts if row["phase"] == phase], event_map, sources),
                       "host_observed": _summary([row for row in measured if row["phase"] == phase], event_map, sources)}
                for phase in PHASES}
    by_provenance = {provenance: _summary([row for row in ordered_attempts
                      if sources[row["source_id"]]["provenance"] == provenance], event_map, sources)
                     for provenance in PROVENANCES}
    host_scope_complete = not unobserved and len(measured) == len(ordered_attempts)
    by_allocation = {allocation: _summary([row for row in measured if row["allocation"] == allocation], event_map, sources,
                                          coverage_complete=host_scope_complete)
                     for allocation in ALLOCATIONS}
    one_time = by_allocation["one_time"]["cost"]["total"]
    denominator = plan["repeat_use_count"]
    allocation = {"repeat_use_count": denominator, "one_time_per_use": None}
    if denominator is not None and one_time is not None and complete:
        allocation["one_time_per_use"] = {"numerator": one_time["amount"], "denominator": denominator,
                                           "currency": one_time["currency"], "pricing_ref": one_time["pricing_ref"]}
    external = _external(plan, _copy([] if external_observations is None else external_observations), sources, used_receipts)
    return _copy({"schema_version": REPORT_VERSION, "scope_id": plan["scope_id"], "plan_sha256": plan["plan_sha256"],
                  "coverage": {"declared_scope_complete": declared_complete,
                               "complete": complete,
                               "host_coverage_authenticated": False, "missing_attempt_ids": missing,
                               "missing_metrics": missing_metrics, "incomplete_source_ids": incomplete_sources,
                               "unknown_status_attempt_ids": unknown_status, "unobserved_phases": unobserved},
                  "automated": _summary(measured, event_map, sources, coverage_complete=host_scope_complete),
                  "by_phase": by_phase, "by_provenance": by_provenance,
                  "by_allocation": by_allocation, "intervals": _intervals(measured, event_map, sources), "allocation": allocation,
                  "external_observations": external, "external_status": _external_status(external, sources),
                  "limitations": ["Coverage and provenance are declarations, not independently authenticated host facts.",
                                  "Elapsed totals add attempt durations; interval union and span have separate coverage.",
                                  "Interval union and span describe attempt activity, not model-wait occupancy.",
                                  "Model-wait seconds add supplied durations; no wait-occupancy union is inferred.",
                                  "Pricing references are declared identities, not verified price tables or bills.",
                                  "Agent work, synthetic records and imported observations do not establish human savings.",
                                  "No future operation cost or adoption outcome is inferred."]})
