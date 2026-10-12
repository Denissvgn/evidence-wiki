"""Frozen release decisions over host-authenticated original owner evidence.

Readers are trusted host code, never selected by a serialized plan. Each reader
must authenticate its existing original owner, source head and live binding before
returning evidence; this module verifies pins and citations, not provider identity.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext

CATEGORIES = ("conformance", "semantic", "integration", "cost", "drift", "uncertainty")
EVIDENCE_CLASSES = ("deterministic_verified", "agent_adjudicated", "simulated", "externally_observed")
OUTCOMES = ("reject", "revise", "inconclusive", "blocked_external", "qualified")
METRICS = ("elapsed_seconds", "model_calls", "tool_calls", "tokens", "cost")
MAX_BYTES = 1_048_576
_JUDGE_FIELDS = {"domain", "rubric_sha256", "model", "settings_sha256", "controller_sha256", "profile"}
_POLICY = {"version": "release-decision-policy/v1", "categories": list(CATEGORIES),
           "precedence": list(OUTCOMES), "profiles": ["technical_fixture", "bounded_semantic"],
           "all_required_sources": True, "critical_failures_allowed": 0,
           "actionable_failures_allowed": 0, "closing_read_required": True,
           "maximum_sources": 32, "maximum_input_bytes": MAX_BYTES}


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def bounded_json(value, maximum=MAX_BYTES):
    """Detach finite JSON after bounding its tree, including cycles and integers."""
    pending, count, size = [(value, 0, frozenset())], 0, 0
    while pending:
        item, depth, ancestors = pending.pop()
        count += 1
        _require(count <= 32768 and depth <= 16, "release_json_bound")
        if type(item) in {dict, list}:
            _require(id(item) not in ancestors and len(item) <= 4096, "release_json_bound")
            parents = ancestors | {id(item)}
            if type(item) is dict:
                _require(all(type(key) is str for key in item), "release_json_shape")
                children = [*item.keys(), *item.values()]
            else:
                children = item
            pending.extend((child, depth + 1, parents) for child in children)
        elif type(item) is str:
            try:
                size += len(item.encode())
            except UnicodeError:
                raise ValueError("release_json_shape") from None
            _require(size <= maximum, "release_json_bound")
        elif type(item) is int:
            _require(item.bit_length() <= 128, "release_json_bound")
        elif type(item) is float:
            _require(math.isfinite(item), "release_json_shape")
        else:
            _require(item is None or type(item) is bool, "release_json_shape")
    raw = canonical(value)
    _require(len(raw) <= maximum, "release_json_bound")
    return json.loads(raw)


def digest(value):
    return hashlib.sha256(canonical(bounded_json(value))).hexdigest()


def policy():
    return bounded_json(_POLICY)


def _object(value, fields, reason):
    _require(type(value) is dict and set(value) == set(fields), reason)
    return value


def _text(value, reason, maximum=4096):
    _require(type(value) is str and 0 < len(value.encode()) <= maximum, reason)


def _sha(value):
    _require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "release_sha256")


def _time(value):
    _require(type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)", value) is not None,
        "release_utc_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("release_utc_time") from None
    _require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0, "release_utc_time")
    return parsed


def _clock(now):
    value = datetime.now(timezone.utc) if now is None else now
    _require(type(value) is datetime and value.tzinfo is not None and value.utcoffset().total_seconds() == 0,
             "release_utc_clock")
    return value


def _metric(value, key):
    if key in {"tokens", "model_calls", "tool_calls"}:
        _require(type(value) is int, "release_metric_type")
    elif type(value) is str:
        _require(re.fullmatch(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,18})?", value) is not None,
                 "release_metric_type")
    else:
        _require(type(value) in {int, float}, "release_metric_type")
        _require(math.isfinite(value), "release_metric_bound")
    number = Decimal(str(value))
    _require(number.is_finite() and 0 <= number <= 1_000_000_000, "release_metric_bound")
    return number


def _sum(values):
    with localcontext() as context:
        context.prec = 64
        return sum(values, Decimal(0))


def _metrics(value, *, budgets=False):
    _object(value, METRICS, "release_metric_shape")
    for key, item in value.items():
        if item is not None:
            _metric(item, key)
    if budgets:
        _require(value["elapsed_seconds"] is not None and _metric(value["elapsed_seconds"], "elapsed_seconds") > 0,
                 "release_elapsed_budget_required")


def _judge(value, *, evidence=False):
    _object(value, _JUDGE_FIELDS | ({"qualified", "expires_at"} if evidence else set()), "release_judge_shape")
    for key in _JUDGE_FIELDS:
        _sha(value[key]) if key.endswith("sha256") else _text(value[key], "release_judge_selector", 256)
    if evidence:
        _require(type(value["qualified"]) is bool, "release_judge_qualification")
        _time(value["expires_at"])


def freeze_plan(*, candidate_sha256, host_sha256, profile, required_sources, budgets,
                scope, frozen_at, expires_at, judge=None):
    """Freeze identities and obligations before outcomes are admitted."""
    plan = {"schema_version": "release-plan/v1", "candidate_sha256": candidate_sha256,
            "host_sha256": host_sha256, "profile": profile, "required_sources": required_sources,
            "budgets": budgets, "scope": scope, "frozen_at": frozen_at, "expires_at": expires_at,
            "judge": judge, "policy": policy(), "policy_sha256": digest(policy())}
    return validate_plan(plan, digest(plan))


def validate_plan(plan, expected_plan_sha256):
    plan = bounded_json(plan)
    _sha(expected_plan_sha256)
    _require(digest(plan) == expected_plan_sha256, "release_plan_pin")
    _object(plan, {"schema_version", "candidate_sha256", "host_sha256", "profile", "required_sources",
                   "budgets", "scope", "frozen_at", "expires_at", "judge", "policy", "policy_sha256"},
            "release_plan_shape")
    _require(plan["schema_version"] == "release-plan/v1", "release_plan_version")
    for key in ("candidate_sha256", "host_sha256", "policy_sha256"):
        _sha(plan[key])
    _require(canonical(plan["policy"]) == canonical(policy()) and plan["policy_sha256"] == digest(policy()),
             "release_policy_pin")
    _require(type(plan["profile"]) is str and plan["profile"] in _POLICY["profiles"], "release_profile")
    _require(type(plan["scope"]) is dict and 1 <= len(plan["scope"]) <= 32, "release_scope")
    _require(_time(plan["frozen_at"]) < _time(plan["expires_at"]), "release_positive_window")
    _metrics(plan["budgets"], budgets=True)
    if plan["judge"] is not None:
        _judge(plan["judge"])
    _require(plan["profile"] != "bounded_semantic" or plan["judge"] is not None, "release_judge_selector_missing")
    sources = plan["required_sources"]
    _require(type(sources) is list and 6 <= len(sources) <= 32, "release_source_inventory")
    seen, categories = set(), set()
    for source in sources:
        _object(source, {"category", "owner_ref", "raw_sha256", "source_head_sha256"}, "release_source_shape")
        _require(type(source["category"]) is str and source["category"] in (*CATEGORIES, "business"), "release_category")
        _text(source["owner_ref"], "release_owner_ref", 512)
        _sha(source["raw_sha256"])
        _sha(source["source_head_sha256"])
        key = source["category"], source["owner_ref"]
        _require(key not in seen, "release_duplicate_source")
        seen.add(key)
        categories.add(source["category"])
    _require(set(CATEGORIES) <= categories, "release_required_categories")
    return plan


class EvidenceUnavailable(Exception):
    """Trusted owner reader cannot supply current required evidence."""


class ExternalBlocked(EvidenceUnavailable):
    """Trusted owner reader observed a required external dependency unavailable."""


@dataclass(frozen=True)
class OriginalCitation:
    pointer: str
    value: object


@dataclass(frozen=True)
class OwnerEvidence:
    raw: bytes
    source_head_sha256: str
    candidate_sha256: str
    host_sha256: str
    scope: dict
    observed_at: str
    expires_at: str
    evidence_class: str
    projection: dict
    citations: tuple[OriginalCitation, ...]


@dataclass(frozen=True)
class OwnerReader:
    category: str
    owner_ref: str
    read: Callable[[], OwnerEvidence]


def _original(raw):
    _require(type(raw) is bytes and len(raw) <= MAX_BYTES, "release_original_bound")
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "release_original_duplicate_key")
            result[key] = value
        return result
    try:
        parsed = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("release_original_nonfinite")))
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError("release_original_json") from None
    return bounded_json(parsed)


def _pointer(original, pointer):
    _require(type(pointer) is str and len(pointer.encode()) <= 4096 and (pointer == "" or pointer.startswith("/")),
             "release_citation_pointer")
    current = original
    if pointer == "":
        return current
    for token in pointer[1:].split("/"):
        _require(re.search(r"~(?![01])", token) is None, "release_citation_pointer")
        token = token.replace("~1", "/").replace("~0", "~")
        if type(current) is dict:
            _require(token in current, "release_citation_missing")
            current = current[token]
        elif type(current) is list:
            _require(re.fullmatch(r"0|[1-9][0-9]{0,9}", token) is not None and int(token) < len(current),
                     "release_citation_missing")
            current = current[int(token)]
        else:
            raise ValueError("release_citation_missing")
    return current


def _business(value, now):
    """Retain actual scoped owner summaries without deriving business value."""
    _object(value, {"kind", "rows", "business_value", "roi"}, "release_business_shape")
    _require(value["kind"] == "outcome_observations" and value["business_value"] == "business_value_unobserved"
             and value["roi"] == "roi_unobserved", "release_business_claim")
    _require(type(value["rows"]) is list and 1 <= len(value["rows"]) <= 64, "release_business_rows")
    subjects = {"customer_adoption": "customer", "sessions": "customer", "incidents": "incident",
                "human_interventions": "human", "human_work_seconds": "human", "payments": "business"}
    fields = {"source", "period", "metric", "unit", "currency", "denominator", "coverage", "gaps", "exclusions",
              "observed_records", "unknown_records", "known_subtotal", "total", "rate", "status", "batch_sha256s"}
    for row in value["rows"]:
        _object(row, fields, "release_business_summary_shape")
        _require(type(row["metric"]) is str and row["metric"] in subjects, "release_business_metric")
        source = _object(row["source"], {"source_id", "origin", "evidence_class", "subject_kind"}, "release_business_source")
        _require(source["evidence_class"] == "externally_observed" and source["subject_kind"] == subjects[row["metric"]],
                 "release_business_not_actual")
        for key in ("source_id", "origin"):
            _text(source[key], "release_business_source")
        period = _object(row["period"], {"started_at", "ended_at"}, "release_business_period")
        _require(_time(period["started_at"]) < _time(period["ended_at"]) <= now, "release_business_period")
        expected_unit = "currency" if row["metric"] == "payments" else "seconds" if row["metric"] == "human_work_seconds" else "count"
        _require(row["unit"] == expected_unit and row["coverage"] in {"complete", "partial", "unknown"}
                 and row["status"] in {"observed", "partial", "unknown"}, "release_business_summary")
        _require(row["currency"] is None if expected_unit != "currency" else
                 type(row["currency"]) is str and re.fullmatch(r"[A-Z]{3}", row["currency"]) is not None,
                 "release_business_currency")
        for key in ("known_subtotal", "total"):
            item = row[key]
            _require((key == "total" and item is None) or (type(item) is str and
                     re.fullmatch(r"(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,9})?", item) is not None),
                     "release_business_decimal")
        for key in ("observed_records", "unknown_records"):
            _metric(row[key], "tokens")
        _require(row["unknown_records"] <= row["observed_records"], "release_business_summary")
        for key in ("gaps", "exclusions", "batch_sha256s"):
            _require(type(row[key]) is list and len(row[key]) <= 64, "release_business_summary")
            for item in row[key]:
                _text(item, "release_business_summary")
        _require(len(row["batch_sha256s"]) > 0 and all(re.fullmatch(r"sha256:[a-f0-9]{64}", item)
                 for item in row["batch_sha256s"]), "release_business_batches")
        denominator = row["denominator"]
        if denominator is not None:
            _object(denominator, {"value", "unit", "population_ref"}, "release_business_population")
            _require(type(denominator["value"]) is int and 0 < denominator["value"] <= 1_000_000_000,
                     "release_business_population")
            _text(denominator["unit"], "release_business_population", 128)
            _text(denominator["population_ref"], "release_business_population")
        if row["rate"] is not None:
            rate = _object(row["rate"], {"numerator", "denominator", "population_ref"}, "release_business_rate")
            _require(denominator is not None and canonical(rate["denominator"]) == canonical(denominator["value"])
                     and rate["population_ref"] == denominator["population_ref"] and rate["numerator"] == row["total"],
                     "release_business_rate")
        if row["status"] == "observed":
            _require(row["coverage"] == "complete" and row["unknown_records"] == 0 and denominator is not None
                     and row["total"] == row["known_subtotal"], "release_business_summary")


def _admit(evidence, source, plan, now):
    _require(type(evidence) is OwnerEvidence, "release_owner_evidence_type")
    original = _original(evidence.raw)
    raw_sha = hashlib.sha256(evidence.raw).hexdigest()
    _require(raw_sha == source["raw_sha256"] and evidence.source_head_sha256 == source["source_head_sha256"],
             "release_original_pin")
    _require(evidence.candidate_sha256 == plan["candidate_sha256"] and evidence.host_sha256 == plan["host_sha256"]
             and canonical(bounded_json(evidence.scope)) == canonical(plan["scope"]), "release_owner_binding")
    observed, expires = _time(evidence.observed_at), _time(evidence.expires_at)
    _require(_time(plan["frozen_at"]) <= observed <= now < expires and observed < expires,
             "release_owner_not_current")
    _require(type(evidence.evidence_class) is str and evidence.evidence_class in EVIDENCE_CLASSES, "release_evidence_class")
    projection = bounded_json(evidence.projection)
    _object(projection, {"status", "metrics", "judge", "business"}, "release_projection_shape")
    _require(type(projection["status"]) is str and projection["status"] in
             {"pass", "critical_failure", "actionable_failure", "unknown"}, "release_owner_status")
    if projection["metrics"] is not None:
        _metrics(projection["metrics"])
    if projection["judge"] is not None:
        _judge(projection["judge"], evidence=True)
    if projection["business"] is not None:
        _require(source["category"] == "business" and evidence.evidence_class == "externally_observed",
                 "release_business_not_actual")
        _business(projection["business"], now)
    _require(type(evidence.citations) is tuple and 1 <= len(evidence.citations) <= 32, "release_citation_inventory")
    citations, seen = [], set()
    for citation in evidence.citations:
        _require(type(citation) is OriginalCitation and citation.pointer not in seen, "release_citation_inventory")
        value = bounded_json(citation.value, 65536)
        _require(canonical(_pointer(original, citation.pointer)) == canonical(value), "release_citation_original_value")
        citations.append({"owner_ref": source["owner_ref"], "raw_sha256": raw_sha,
                          "pointer": citation.pointer, "value": value})
        seen.add(citation.pointer)
    if projection["business"] is not None:
        for row in projection["business"]["rows"]:
            _require(any(canonical(row) == canonical(citation["value"]) for citation in citations),
                     "release_business_original_summary")
    return {"category": source["category"], "owner_ref": source["owner_ref"], "raw_sha256": raw_sha,
            "source_head_sha256": evidence.source_head_sha256, "evidence_class": evidence.evidence_class,
            "projection": projection, "citations": citations}


def _semantic_eligible(evidence, plan, now):
    if plan["profile"] == "technical_fixture":
        return evidence["evidence_class"] == "simulated"
    judge = evidence["projection"]["judge"]
    return (evidence["evidence_class"] == "agent_adjudicated" and judge is not None
            and judge["qualified"] is True and now < _time(judge["expires_at"])
            and canonical({key: judge[key] for key in _JUDGE_FIELDS}) == canonical(plan["judge"]))


def _reason(code, category, citations=()):
    return {"code": code, "category": category, "citations": list(citations)}


def decide(plan, expected_plan_sha256, readers, now=None):
    """Read each exact owner twice and refuse stale, changed or withdrawn facts."""
    plan = validate_plan(plan, expected_plan_sha256)
    opening = _clock(now)
    _require(type(readers) in {list, tuple} and len(readers) <= 32, "release_reader_inventory")
    registry = {}
    for reader in readers:
        _require(type(reader) is OwnerReader and callable(reader.read) and type(reader.category) is str
                 and type(reader.owner_ref) is str, "release_reader_type")
        key = reader.category, reader.owner_ref
        _require(key not in registry, "release_duplicate_reader")
        registry[key] = reader
    required = {(source["category"], source["owner_ref"]) for source in plan["required_sources"]}
    _require(set(registry) <= required, "release_unrequested_reader")
    findings, admitted, business_warnings = [], [], []
    live_plan = _time(plan["frozen_at"]) <= opening < _time(plan["expires_at"])
    if not live_plan:
        findings.append(("inconclusive", _reason("plan_not_current", "all")))
    ordered = sorted(plan["required_sources"], key=lambda row: (row["category"], row["owner_ref"]))
    snapshots, problems, expiries = {}, {}, {}
    for closing_pass in (False, True):
        for source in ordered:
            key = source["category"], source["owner_ref"]
            reader = registry.get(key)
            if reader is None:
                problems[key] = "owner_missing"
                continue
            try:
                native = reader.read()
                read_time = _clock(now)
                evidence = _admit(native, source, plan, read_time)
                if closing_pass:
                    _require(key in snapshots and canonical(snapshots[key]) == canonical(evidence),
                             "release_owner_changed")
                    expiries[key] = _time(native.expires_at)
                else:
                    snapshots[key] = evidence
                # Observed native failures retain their stronger decision precedence.
                if (source["category"] != "business" and (source["category"] != "semantic"
                        or _semantic_eligible(evidence, plan, read_time))) and evidence["projection"]["status"] in {
                        "critical_failure", "actionable_failure"}:
                    status = evidence["projection"]["status"]
                    finding = ("reject" if status == "critical_failure" else "revise",
                               _reason("owner_" + status, source["category"], evidence["citations"]))
                    if finding not in findings:
                        findings.append(finding)
            except ExternalBlocked:
                problems[key] = "external_unavailable"
            except EvidenceUnavailable:
                problems[key] = "owner_unavailable"
            except Exception:
                problems[key] = "owner_invalid_or_changed"
    opening_costs = [row for row in snapshots.values() if row["category"] == "cost" and row["projection"]["metrics"] is not None]
    for metric in METRICS:
        cap = plan["budgets"][metric]
        known = [row for row in opening_costs if row["projection"]["metrics"][metric] is not None]
        subtotal = _sum(_metric(row["projection"]["metrics"][metric], metric) for row in known)
        if cap is not None and subtotal > _metric(cap, metric):
            findings.append(("revise", _reason("budget_exceeded_" + metric, "cost",
                [citation for row in known for citation in row["citations"]])))
    final_clock = _clock(now)
    cost_totals = {key: Decimal(0) for key in METRICS}
    cost_unknown = set()
    cost_citations = []
    for source in ordered:
        category = source["category"]
        key = category, source["owner_ref"]
        problem = problems.get(key)
        if problem is None and final_clock >= expiries[key]:
            problem = "owner_expired_at_close"
        if problem is not None:
            if category == "business":
                business_warnings.append(problem)
            if category != "business":
                findings.append(("blocked_external" if problem == "external_unavailable" else "inconclusive",
                                 _reason(problem, category)))
            continue
        first = snapshots[key]
        admitted.append(first)
        projection, citations = first["projection"], first["citations"]
        if category == "business":
            continue
        status = projection["status"]
        if status == "unknown":
            findings.append(("inconclusive", _reason("owner_unknown", category, citations)))
        if category == "semantic" and not _semantic_eligible(first, plan, final_clock):
            code = "semantic_judge_unqualified" if plan["profile"] == "bounded_semantic" else "fixture_semantic_scope_required"
            findings.append(("inconclusive", _reason(code, category, citations)))
        if category == "cost":
            if projection["metrics"] is None:
                findings.append(("inconclusive", _reason("usage_unknown", category, citations)))
            else:
                cost_citations.extend(citations)
                for key in METRICS:
                    if projection["metrics"][key] is None:
                        cost_unknown.add(key)
                    else:
                        cost_totals[key] = _sum((cost_totals[key], _metric(projection["metrics"][key], key)))
    for key in METRICS:
        cap = plan["budgets"][key]
        if cap is None:
            continue
        if key in cost_unknown:
            findings.append(("inconclusive", _reason("usage_unknown_" + key, "cost", cost_citations)))
        elif cost_totals[key] > _metric(cap, key):
            finding = ("revise", _reason("budget_exceeded_" + key, "cost", cost_citations))
            if finding not in findings:
                findings.append(finding)
    closing = _clock(now)
    for item in admitted:
        key = item["category"], item["owner_ref"]
        if closing >= expiries[key]:
            if item["category"] == "business":
                business_warnings.append("owner_expired_at_close")
            else:
                findings.append(("inconclusive", _reason("owner_expired_at_close", item["category"], item["citations"])))
        if item["category"] == "semantic" and not _semantic_eligible(item, plan, closing):
            findings.append(("inconclusive", _reason("semantic_judge_unqualified_at_close", "semantic", item["citations"])))
    if not (_time(plan["frozen_at"]) <= closing < _time(plan["expires_at"])):
        findings.append(("inconclusive", _reason("plan_not_current_at_close", "all")))
    outcome = next((candidate for candidate in OUTCOMES if any(item[0] == candidate for item in findings)), "qualified")
    reasons = [reason for _, reason in sorted(findings, key=lambda item: (OUTCOMES.index(item[0]), item[1]["category"], item[1]["code"]))]
    if outcome == "qualified":
        reasons = [_reason("all_required_owners_passed", "all", [citation for item in admitted for citation in item["citations"]])]
    report = {"schema_version": "release-decision/v1", "plan": plan, "plan_sha256": expected_plan_sha256,
              "outcome": outcome, "claim_scope": "fixture_mechanics" if plan["profile"] == "technical_fixture" else "bounded_semantic",
              "reasons": reasons, "evidence": admitted,
              "business": {"business_value": "business_value_unobserved", "roi": "roi_unobserved",
                           "warnings": sorted(set(business_warnings))}, "authorized": False,
              "evidence_accepted": False, "release_published": False}
    report["report_sha256"] = digest(report)
    return bounded_json(report)


def replay_decision(report, expected_plan_sha256, readers, now=None):
    """Verify report integrity, then read current original owners again."""
    report = bounded_json(report)
    _object(report, {"schema_version", "plan", "plan_sha256", "outcome", "claim_scope", "reasons", "evidence",
                     "business", "authorized", "evidence_accepted", "release_published", "report_sha256"}, "release_report_shape")
    recorded_sha = report["report_sha256"]
    payload = {key: value for key, value in report.items() if key != "report_sha256"}
    _require(digest(payload) == recorded_sha and report["plan_sha256"] == expected_plan_sha256, "release_report_pin")
    replayed = decide(report["plan"], expected_plan_sha256, readers, now)
    _require(canonical(replayed) == canonical(report), "release_replay_changed")
    return replayed
