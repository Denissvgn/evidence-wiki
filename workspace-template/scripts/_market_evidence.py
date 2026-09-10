#!/usr/bin/env python3
"""Bounded, inert filing and price deliveries with exact decimal observations."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from _evidence_authority import EvidenceInvalid, bounded_list, digest, exact_object, name, timestamp
from _normalized_contract import safe_source_id
from _record_artifacts import capture_artifacts, closure_identity, file_binding, json_document, validate_members
from _workspace_module_loader import load_workspace_module

PROFILE = "market_evidence/v1"
SCHEMA = "market-evidence/v1"
RECORD = "market-record.json"
MAX_PAGES = 16
MAX_OBSERVATIONS = 4096
MAX_LISTINGS = 64
ROUTES = ("sec-company-concept", "alpaca-stock-bars")


def profile_description() -> dict[str, Any]:
    return {"name": PROFILE, "record_schema": SCHEMA, "routes": list(ROUTES),
            "network_io_executed": False, "producer_execution": False,
            "execution": "host_delegated_inert_delivery", "authority": "external_usage_policy_required",
            "bounds": {"pages": MAX_PAGES, "observations": MAX_OBSERVATIONS, "listings": MAX_LISTINGS},
            "capabilities": ["exact_decimal_observations", "requested_delivered_scope", "explicit_completeness_gaps"]}


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceInvalid(reason)


def text(value: Any, maximum: int = 256) -> str:
    require(isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value), "invalid_market_text")
    return value


def day(value: Any) -> str:
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None, "invalid_market_date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceInvalid("invalid_market_date") from exc
    return value


def decimal(value: Any) -> str:
    """No binary float round-trip, non-finite values, or unbounded exponents."""
    require(type(value) in {int, str, Decimal}, "invalid_market_decimal")
    raw = str(value)
    require(len(raw) <= 128, "market_decimal_bound")
    try:
        number = Decimal(raw)
    except InvalidOperation as exc:
        raise EvidenceInvalid("invalid_market_decimal") from exc
    require(number.is_finite() and len(number.as_tuple().digits) <= 64 and abs(number.as_tuple().exponent) <= 32,
            "market_decimal_bound")
    return format(number, "f")


def provider_document(data: bytes) -> dict[str, Any]:
    # The common parser owns duplicate-key/depth/size rejection. Reparse only
    # after that check to retain the provider's numeric decimal tokens exactly.
    json_document(data)
    try:
        value = json.loads(data, parse_float=Decimal, parse_int=int,
                           parse_constant=lambda _value: (_ for _ in ()).throw(EvidenceInvalid("invalid_market_decimal")))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise EvidenceInvalid("invalid_market_provider_json") from exc
    return value


def profile_for(record: dict[str, Any], normalized: dict[str, Any] | None = None) -> str | None:
    metadata = record.get("metadata") or {}
    choices = []
    if isinstance(metadata, dict) and "market_profile" in metadata:
        choices.append(metadata["market_profile"])
    if record.get("kind") == "market_evidence":
        choices.append(PROFILE)
    if normalized and normalized.get("market_evidence") is not None:
        value = normalized["market_evidence"]
        choices.append(value.get("profile") if isinstance(value, dict) else None)
    if not choices:
        return None
    require(all(choice == PROFILE for choice in choices), "unsupported_market_profile")
    return PROFILE


def reference(value: Any, members: dict[str, Any], used: set[str]) -> str:
    exact_object(value, {"path", "content_hash"})
    path = text(value["path"])
    require(path in members and digest(value["content_hash"]) == members[path]["content_hash"], "market_artifact_binding_mismatch")
    used.add(path)
    return path


def listings(value: Any, gaps: set[str]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for item in bounded_list(value, maximum=MAX_LISTINGS):
        exact_object(item, {"issuer_id", "listing_id", "provider_id", "venue", "symbol", "issuer_name",
                            "valid_from", "valid_to", "currency", "status", "status_at"})
        for key in ("issuer_id", "listing_id", "provider_id", "venue", "symbol"):
            name(item[key])
        text(item["issuer_name"])
        require(re.fullmatch(r"[A-Z]{3}", text(item["currency"])) is not None, "invalid_market_currency")
        require(item["status"] in {"active", "inactive", "delisted", "unknown"}, "invalid_listing_status")
        start = timestamp(item["valid_from"])
        end = timestamp(item["valid_to"]) if item["valid_to"] is not None else None
        timestamp(item["status_at"])
        require(end is None or start < end, "invalid_ticker_interval")
        key = (item["listing_id"], item["valid_from"], item["venue"], item["symbol"])
        require(key not in seen, "duplicate_listing_interval")
        seen.add(key)
        if item["status"] == "unknown":
            gaps.add("listing_status_unknown")
        for previous in result:
            if (previous["listing_id"] == item["listing_id"]
                    and any(previous[key] != item[key] for key in ("issuer_id", "provider_id", "venue", "currency"))):
                gaps.add("listing_identity_conflict")
        result.append(dict(item))
    return result


def scope(value: Any, members: dict[str, Any], used: set[str], gaps: set[str]) -> dict[str, Any]:
    exact_object(value, {"provider", "retrieved_at", "policy_reference", "durable_slice_id", "retrieval_instructions",
                         "conflicts", "excluded", "universe", "corporate_action_coverage", "corporate_actions"})
    name(value["provider"])
    timestamp(value["retrieved_at"])
    name(value["durable_slice_id"])
    text(value["retrieval_instructions"], 4096)
    if value["policy_reference"] is None:
        gaps.add("usage_policy_evidence_missing")
    else:
        policy = exact_object(value["policy_reference"], {"policy_id", "policy_revision", "artifact"})
        name(policy["policy_id"])
        name(policy["policy_revision"])
        reference(policy["artifact"], members, used)
    for key in ("conflicts", "excluded"):
        for item in bounded_list(value[key], maximum=128):
            text(item, 1024)
        if value[key]:
            gaps.add("provider_value_conflict" if key == "conflicts" else "observations_excluded")
    universe = exact_object(value["universe"], {"as_of", "coverage", "listing_ids", "artifact"})
    timestamp(universe["as_of"])
    require(universe["coverage"] in {"complete", "partial", "survivors-only", "unknown", "not-applicable"}, "invalid_universe_coverage")
    identities = bounded_list(universe["listing_ids"], maximum=MAX_LISTINGS)
    require(len(set(name(item) for item in identities)) == len(identities), "duplicate_universe_listing")
    if universe["artifact"] is not None:
        reference(universe["artifact"], members, used)
    if universe["coverage"] not in {"complete", "not-applicable"} or universe["coverage"] == "complete" and universe["artifact"] is None:
        gaps.add("historical_universe_incomplete")
    require(value["corporate_action_coverage"] in {"complete", "partial", "unknown", "not-applicable"}, "invalid_corporate_action_coverage")
    if value["corporate_action_coverage"] in {"partial", "unknown"}:
        gaps.add("corporate_action_lineage_incomplete")
    for action in bounded_list(value["corporate_actions"], maximum=256):
        exact_object(action, {"listing_id", "type", "effective_at", "ratio", "amount", "currency", "artifact"})
        name(action["listing_id"])
        timestamp(action["effective_at"])
        require(action["type"] in {"split", "dividend", "spin-off"}, "invalid_corporate_action")
        reference(action["artifact"], members, used)
        if action["type"] == "split":
            require(Decimal(decimal(action["ratio"])) > 0 and action["amount"] is None and action["currency"] is None, "invalid_split_ratio")
        elif action["type"] == "dividend":
            require(action["ratio"] is None and Decimal(decimal(action["amount"])) >= 0
                    and re.fullmatch(r"[A-Z]{3}", text(action["currency"])) is not None, "invalid_dividend")
        else:
            require(action["ratio"] is None and action["amount"] is None and action["currency"] is None, "invalid_spin_off")
    return value


def filing_observations(request: Any, pages: list[dict[str, Any]], identities: list[dict[str, Any]], gaps: set[str]) -> list[dict[str, Any]]:
    exact_object(request, {"issuer_id", "cik", "taxonomy", "tag", "units", "periods", "accessions"})
    name(request["issuer_id"])
    require(isinstance(request["cik"], str) and re.fullmatch(r"\d{10}", request["cik"]) is not None, "invalid_filing_cik")
    name(request["taxonomy"])
    name(request["tag"])
    units = bounded_list(request["units"], maximum=16, minimum=1)
    require(len(set(name(unit) for unit in units)) == len(units), "duplicate_filing_unit")
    periods = bounded_list(request["periods"], maximum=64, minimum=1)
    for period in periods:
        exact_object(period, {"start", "end"})
        day(period["end"])
        if period["start"] is not None:
            require(day(period["start"]) <= period["end"], "invalid_fiscal_period")
    accessions = bounded_list(request["accessions"], maximum=128)
    require(len(set(text(item) for item in accessions)) == len(accessions), "duplicate_filing_revision")
    require(len(pages) == 1, "filing_route_does_not_paginate")
    page = pages[0]
    require(type(page.get("cik")) is int and f"{page['cik']:010d}" == request["cik"]
            and page.get("taxonomy") == request["taxonomy"] and page.get("tag") == request["tag"], "filing_provider_identity_mismatch")
    text(page.get("entityName"))
    if any(item["issuer_id"] != request["issuer_id"] for item in identities):
        gaps.add("filing_listing_issuer_mismatch")
    values = page.get("units")
    require(isinstance(values, dict) and len(values) <= 32, "invalid_filing_units")
    result = []
    observed = set()
    examined = 0
    for unit, facts in values.items():
        name(unit)
        for fact in bounded_list(facts, maximum=MAX_OBSERVATIONS):
            examined += 1
            require(examined <= MAX_OBSERVATIONS, "market_observation_bound")
            require(isinstance(fact, dict), "invalid_filing_fact")
            end, start = day(fact.get("end")), fact.get("start")
            if start is not None:
                require(day(start) <= end, "invalid_fiscal_period")
            if unit not in units or {"start": start, "end": end} not in periods:
                continue
            accession = text(fact.get("accn"))
            if accessions and accession not in accessions:
                continue
            require(type(fact.get("fy")) is int and 1900 <= fact["fy"] <= 9999, "invalid_fiscal_year")
            item = {"issuer_id": request["issuer_id"], "cik": request["cik"], "issuer_name": page["entityName"],
                    "taxonomy": request["taxonomy"], "tag": request["tag"], "unit": unit, "scale": 0,
                    "value": decimal(fact.get("val")), "period_start": start, "period_end": end,
                    "accession": accession, "form": text(fact.get("form")), "filed_date": day(fact.get("filed")),
                    "fiscal_year": fact["fy"], "fiscal_period": text(fact.get("fp")), "frame": fact.get("frame"),
                    "available_at": None}
            if item["frame"] is not None:
                text(item["frame"])
            key = (unit, start, end, accession)
            if key in observed:
                gaps.add("duplicate_or_conflicting_filing_fact")
            observed.add(key)
            result.append(item)
            require(len(result) <= MAX_OBSERVATIONS, "market_observation_bound")
    for unit in units:
        for period in periods:
            if not any(item["unit"] == unit and item["period_start"] == period["start"] and item["period_end"] == period["end"] for item in result):
                gaps.add("requested_filing_observation_missing")
    if set(accessions) - {item["accession"] for item in result}:
        gaps.add("requested_filing_revision_missing")
    return result


def price_observations(request: Any, pages: list[dict[str, Any]], identities: list[dict[str, Any]], provenance: dict[str, Any], gaps: set[str]) -> list[dict[str, Any]]:
    exact_object(request, {"symbols", "start", "end", "timeframe", "feed", "delay", "currency", "adjustment",
                           "asof", "session", "calendar", "timezone", "expected_bars"})
    symbols = bounded_list(request["symbols"], maximum=MAX_LISTINGS, minimum=1)
    require(len(set(name(symbol) for symbol in symbols)) == len(symbols), "duplicate_requested_symbol")
    start, end = timestamp(request["start"]), timestamp(request["end"])
    require(start < end, "invalid_market_request_interval")
    for key in ("timeframe", "feed", "session", "calendar", "timezone"):
        text(request[key])
    require(request["delay"] in {"real-time", "delayed", "end-of-day", "unknown"}, "invalid_feed_delay")
    if request["delay"] == "unknown":
        gaps.add("feed_delay_unknown")
    require(re.fullmatch(r"[A-Z]{3}", text(request["currency"])) is not None, "invalid_market_currency")
    adjustments = text(request["adjustment"]).split(",")
    require(set(adjustments) <= {"raw", "split", "dividend", "spinoff", "all"} and len(set(adjustments)) == len(adjustments)
            and (len(adjustments) == 1 or not set(adjustments) & {"raw", "all"}), "invalid_price_adjustment")
    # The provider's date selects symbol mapping; it is never availability proof.
    mapping_date = day(request["asof"]) if request["asof"] != "-" else None
    mapping_time = datetime.combine(date.fromisoformat(mapping_date), datetime.min.time(), timezone.utc) if mapping_date else None
    try:
        ZoneInfo(request["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise EvidenceInvalid("invalid_market_timezone") from exc
    expected = bounded_list(request["expected_bars"], maximum=MAX_OBSERVATIONS, minimum=1)
    by_key = {}
    for item in expected:
        exact_object(item, {"listing_id", "symbol", "start", "end"})
        name(item["listing_id"])
        require(item["symbol"] in symbols, "unexpected_bar_symbol")
        left, right = timestamp(item["start"]), timestamp(item["end"])
        require(start <= left < right <= end, "invalid_expected_bar_interval")
        key = (item["symbol"], left)
        require(key not in by_key, "duplicate_expected_bar")
        by_key[key] = item
    universe = provenance["universe"]
    if universe["coverage"] == "not-applicable" or not universe["listing_ids"]:
        gaps.add("historical_universe_incomplete")
    if timestamp(universe["as_of"]) > start:
        gaps.add("universe_observed_after_request_start")
    if provenance["corporate_action_coverage"] == "not-applicable":
        gaps.add("corporate_action_lineage_incomplete")
    for action in provenance["corporate_actions"]:
        matching = [item for item in identities if item["listing_id"] == action["listing_id"]]
        if not matching or action["listing_id"] not in universe["listing_ids"]:
            gaps.add("corporate_action_identity_unknown")
        elif action["type"] == "dividend" and any(item["currency"] != action["currency"] for item in matching):
            gaps.add("corporate_action_currency_mismatch")
    seen, result = set(), []
    for page in pages:
        if "next_page_token" not in page:
            gaps.add("provider_pagination_completion_unknown")
        bars = page.get("bars")
        require(isinstance(bars, dict) and len(bars) <= MAX_LISTINGS, "invalid_price_response")
        for symbol, observations in bars.items():
            require(symbol in symbols, "unrequested_price_symbol")
            for bar in bounded_list(observations, maximum=MAX_OBSERVATIONS):
                exact_object(bar, {"t", "o", "h", "l", "c", "v", "n", "vw"})
                at = timestamp(bar["t"])
                key = (symbol, at)
                require(key in by_key, "unrequested_price_bar")
                expected_bar = by_key[key]
                if key in seen:
                    gaps.add("duplicate_or_conflicting_price_bar")
                seen.add(key)
                when = mapping_time or at
                until = when + timedelta(days=1) if mapping_date else when
                matches = [item for item in identities if item["symbol"] == symbol
                           and timestamp(item["valid_from"]) <= until
                           and (item["valid_to"] is None or when < timestamp(item["valid_to"]))]
                if mapping_date and any(timestamp(item["valid_from"]) > when
                                        or item["valid_to"] is not None and timestamp(item["valid_to"]) < until for item in matches):
                    gaps.add("symbol_mapping_date_ambiguous")
                if len(matches) != 1 or matches[0]["listing_id"] != expected_bar["listing_id"]:
                    gaps.add("ticker_identity_ambiguous")
                elif matches[0]["currency"] != request["currency"]:
                    gaps.add("listing_price_currency_mismatch")
                values = {key: decimal(bar[key]) for key in ("o", "h", "l", "c", "v")}
                values["vw"] = decimal(bar["vw"]) if bar["vw"] is not None else None
                require(type(bar["n"]) is int and bar["n"] >= 0 and Decimal(values["v"]) >= 0, "invalid_bar_count")
                require(Decimal(values["l"]) <= min(Decimal(values["o"]), Decimal(values["c"]))
                        and Decimal(values["h"]) >= max(Decimal(values["o"]), Decimal(values["c"]))
                        and Decimal(values["l"]) >= 0, "invalid_ohlc_bounds")
                completed = timestamp(expected_bar["end"]) <= timestamp(provenance["retrieved_at"])
                if not completed:
                    gaps.add("price_bar_incomplete")
                result.append({"listing_id": expected_bar["listing_id"], "symbol": symbol, "start": bar["t"],
                               "end": expected_bar["end"], "complete": completed, "currency": request["currency"],
                               "price_unit": request["currency"] + "/share", "volume_unit": "shares", "scale": 0,
                               "adjustment": request["adjustment"], "feed": request["feed"], "delay": request["delay"],
                               "open": values["o"], "high": values["h"], "low": values["l"], "close": values["c"],
                               "volume": values["v"], "trade_count": bar["n"], "vwap": values["vw"]})
                require(len(result) <= MAX_OBSERVATIONS, "market_observation_bound")
    if set(by_key) - seen:
        gaps.add("requested_price_observation_missing")
    if set(universe["listing_ids"]) - {item["listing_id"] for item in result}:
        gaps.add("universe_member_observation_missing")
    if {item["listing_id"] for item in result} - set(universe["listing_ids"]):
        gaps.add("price_listing_outside_universe")
    return result


def validate_closure(source_id: str, files: dict[str, bytes]) -> dict[str, Any]:
    require(RECORD in files, "market_record_missing")
    document = exact_object(json_document(files[RECORD]), {"schema_version", "profile", "source_id", "route", "request",
                                                         "artifacts", "pages", "listings", "provenance"})
    require(document["schema_version"] == SCHEMA and document["profile"] == PROFILE, "unsupported_market_schema")
    require(document["source_id"] == source_id, "market_source_identity_mismatch")
    require(document["route"] in ROUTES, "unsupported_market_route")
    members = validate_members(document["artifacts"], files, RECORD)
    used: set[str] = set()
    gaps: set[str] = set()
    identities = listings(document["listings"], gaps)
    provenance = scope(document["provenance"], members, used, gaps)
    pages = []
    tokens = set()
    previous = None
    for index, page in enumerate(bounded_list(document["pages"], maximum=MAX_PAGES, minimum=1)):
        exact_object(page, {"artifact", "request_token"})
        token = page["request_token"]
        if token is not None:
            text(token, 4096)
        require(token == previous and (index == 0 or token is not None) and token not in tokens, "market_pagination_chain_invalid")
        tokens.add(token)
        path = reference(page["artifact"], members, used)
        payload = provider_document(files[path])
        previous = payload.get("next_page_token")
        if previous is not None:
            text(previous, 4096)
        pages.append(payload)
    if previous is not None:
        gaps.add("provider_pagination_incomplete")
    if document["route"] == "sec-company-concept":
        require(provenance["provider"] == "sec", "market_provider_route_mismatch")
        values = filing_observations(document["request"], pages, identities, gaps)
    else:
        require(provenance["provider"] == "alpaca", "market_provider_route_mismatch")
        values = price_observations(document["request"], pages, identities, provenance, gaps)
    require(set(members) == used, "market_unreferenced_artifacts")
    data = {"route": document["route"], "requested": document["request"], "listings": identities,
            "provenance": provenance, "observations": values, "complete": not gaps, "gaps": sorted(gaps)}
    return {"schema_version": "market-evidence-validation/v1", "profile": PROFILE, "source_id": source_id,
            "valid": True, "reason": "market_structure_valid", "closure_id": closure_identity(files),
            "originals": {path: file_binding(value) for path, value in sorted(files.items())}, "data": data,
            "completeness": {"complete": not gaps, "gaps": sorted(gaps), "pages": len(pages), "observations": len(values),
                             "continuation": previous, "bounds": profile_description()["bounds"]},
            "authority": "not_evaluated", "availability": "requires_temporal_qualification"}


def inspect_market(root: Path, config: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    try:
        require(profile_for(record) is not None, "market_profile_not_selected")
        usage = load_workspace_module(Path(__file__).resolve().parent, "_usage_gate")
        files = usage.original_artifacts(root, config, record)
        if files is None:
            files = capture_artifacts(root, f"sources/evidence/{safe_source_id(record['id'])}")
        return validate_closure(record["id"], files)
    except EvidenceInvalid as exc:
        reason = str(exc)
    except (OSError, KeyError, ValueError, TypeError, AttributeError, RecursionError):
        reason = "invalid_market_evidence"
    return {"schema_version": "market-evidence-validation/v1", "profile": PROFILE, "source_id": record.get("id"),
            "valid": False, "reason": reason, "authority": "not_evaluated"}


def normalized_issues(root: Path | None, config: dict[str, Any], record: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    try:
        if profile_for(record, normalized) is None:
            return []
    except EvidenceInvalid as exc:
        return [str(exc)]
    if root is None:
        return ["market_original_context_required"]
    report = inspect_market(root, config, record)
    if report != normalized.get("market_evidence"):
        return ["market_normalized_binding_mismatch"]
    if not report["valid"]:
        return [report["reason"]]
    rendered = scalar_bytes(report)
    structured = normalized.get("structured_view")
    if not isinstance(structured, dict) or structured.get("content_hash") != "sha256:" + hashlib.sha256(rendered).hexdigest():
        return ["market_structured_binding_mismatch"]
    return []


def scalar_bytes(report: dict[str, Any]) -> bytes:
    return (json.dumps(report["data"], ensure_ascii=False, indent=2, sort_keys=True,
                       separators=(",", ": "), allow_nan=False) + "\n").encode("utf-8")


def validate_captured(report: dict[str, Any], metadata: dict[str, Any], files: dict[str, bytes], record_path: str | None) -> None:
    """Validate normalized and scalar commitments against independently parsed originals."""
    require(profile_for({}, metadata) == PROFILE and metadata.get("market_evidence") == report,
            "market_normalized_binding_mismatch")
    rendered = scalar_bytes(report)
    structured = metadata.get("structured_view")
    require(isinstance(structured, dict) and structured.get("content_hash") == file_binding(rendered)["content_hash"]
            and rendered in files.values(), "market_structured_binding_mismatch")
    if record_path is not None:
        require(record_path in files and json_document(files[record_path]) == report["data"], "market_temporal_scalar_mismatch")


def qualify_cutoff(report: dict[str, Any], times: dict[str, Any], cutoff: datetime, observed: datetime) -> None:
    """Keep complete observations inside the host-qualified measurement and availability interval."""
    require(report["completeness"]["complete"], "market_scope_incomplete")
    data = report["data"]
    retrieved = timestamp(data["provenance"]["retrieved_at"])
    require(times["available"] <= retrieved <= observed and (times["retrieved"] is None or times["retrieved"] == retrieved),
            "market_retrieval_time_mismatch")
    measured = times["measurement"]
    require(measured is not None and all(value is not None for value in measured), "market_measurement_interval_required")
    if data["route"] == "alpaca-stock-bars":
        require(all(measured[0] <= timestamp(item["start"]) < timestamp(item["end"]) <= measured[1]
                    and timestamp(item["end"]) <= times["published"] for item in data["observations"]),
                "market_bar_outside_qualified_interval")
        require(timestamp(data["provenance"]["universe"]["as_of"]) <= cutoff
                and all(timestamp(item["status_at"]) <= cutoff for item in data["listings"])
                and all(timestamp(item["effective_at"]) <= cutoff for item in data["provenance"]["corporate_actions"]),
                "market_future_identity_or_adjustment")
    else:
        require(all(date.fromisoformat(item["period_end"]) <= measured[1].date()
                    and (item["period_start"] is None or measured[0].date() <= date.fromisoformat(item["period_start"]))
                    and date.fromisoformat(item["filed_date"]) <= times["published"].date() for item in data["observations"]),
                "market_filing_outside_qualified_interval")


def consumer_issues(root: Path | None, config: dict[str, Any], record: dict[str, Any], normalized: dict[str, Any]) -> list[str]:
    issues = normalized_issues(root, config, record, normalized)
    if issues:
        return issues
    if normalized.get("market_evidence") is None:
        return []
    usage = load_workspace_module(Path(__file__).resolve().parent, "_usage_gate")
    if not usage.configured(config):
        return ["usage_authority_required"]
    return list(normalized["market_evidence"]["completeness"]["gaps"])
