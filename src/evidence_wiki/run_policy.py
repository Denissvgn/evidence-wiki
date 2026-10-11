"""Immutable host policy and deterministic, inert decisions from owner receipts.

The host must authenticate observations before calling this module. References
and booleans cannot authenticate their producer. No decision dispatches work,
grants authority, accepts evidence, signs a receipt or changes active policy.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, NoReturn

from .workflow_contracts import WorkflowContractError

POLICY = "research-run-policy/v1"
POLICY_V2 = "research-run-policy/v2"
POLICY_V3 = "research-run-policy/v3"
OBSERVATION = "research-policy-observation/v1"
OBSERVATION_V2 = "research-policy-observation/v2"
DECISION = "research-policy-decision/v1"
MAX_BYTES = 262_144
MAX_DEPTH = 12
MAX_NODES = 8192
EFFECTS = ("offline_check", "model", "acquire", "revise", "retry", "adjudicate", "deliver")
TERMINAL_OUTCOMES = ("accepted", "partial", "abstained", "rejected", "stop_budget", "blocked_external", "policy_incompatible")
CONTINUATION_OUTCOMES = ("revise", "acquire_more", "retry")
_REASONS = (
    "invalid_input", "policy_binding", "scope_accounting", "untrusted_observation", "human_review_required",
    "authority_unavailable", "executor_unavailable", "usage_unknown", "budget_exhausted", "model_budget_exhausted",
    "basis_changed", "qualification_unknown", "mechanical_failure", "unsupported_claim", "review_disagreement",
    "acquisition_ungranted", "insufficient_evidence", "effect_ungranted", "current_outcomes", "partial_disallowed",
)
_COUNTERS = ("repair_cycles", "adjudication_cycles", "retry_attempts", "acquisition_attempts")
_FLAGS = ("source_current", "source_present", "mechanical_pass", "review_pass", "qualified", "context_complete")


def _refuse(reason: str, path: str = "/") -> NoReturn:
    raise WorkflowContractError(
        f"RUN_POLICY_{reason}", "Run policy contract refused the input.", recoverable=False,
        remediation="Use the closed schema, frozen policy identity and authenticated current canonical owner observations.",
        details={"field": path},
    ) from None


def _object(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _string(maximum: int = 256) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"^[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*$(?![\s\S])"}


def _integer(maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _nullable(shape: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape: dict[str, Any], maximum: int = 64, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": shape, "minItems": minimum, "maxItems": maximum}


def _build_schemas() -> dict[str, dict[str, Any]]:
    """Closed policy, receipt and inert decision shapes; semantics are checked in code."""
    ident = _string()
    policy_id = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    money = {"type": "string", "maxLength": 20, "pattern": r"^(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?$(?![\s\S])"}
    ratio = {"type": "string", "maxLength": 8, "pattern": r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$(?![\s\S])"}
    known = {"type": "boolean"}
    flag = _nullable(known)
    role = {"enum": ["evaluator", "critic", "adjudicator"]}
    assurance = {"enum": ["artifact_checked", "host_enforced"]}
    counters = _object(**{name: _integer(8 if name != "adjudication_cycles" else 4) for name in _COUNTERS})
    budget = _object(seconds=_integer(86_400, 1), model_calls=_integer(1000), tool_calls=_integer(10_000),
                     tokens=_integer(10_000_000), cost_limit=money, questions=_integer(100, 1),
                     claims=_integer(10_000), sources=_integer(10_000))
    thresholds = _object(
        critical_false_acceptances_max={"const": 0}, min_reference_cases=_integer(1_000_000, 100),
        min_families=_integer(10_000, 10), positive_acceptance_min=ratio, meaning_preserving_flip_max=ratio,
    )
    policy = _object(
        schema_version={"const": POLICY}, policy_id=_nullable(policy_id),
        objective={"type": "string", "minLength": 1, "maxLength": 65_536},
        scope=_object(question_ids={**_array(ident, 100, 1), "uniqueItems": True},
                      source_scope={**_array(_string(4096), 64, 1), "uniqueItems": True}),
        allowed_effects={**_array({"enum": list(EFFECTS)}, len(EFFECTS)), "uniqueItems": True},
        host_capability_refs=_object(**{effect: _nullable(ident) for effect in EFFECTS if effect != "offline_check"}),
        assurance=assurance, human_review=known,
        required_evaluators=_array(_object(role=role, profile_ref=ident, controller_ref=ident, human_required=known), 8, 1),
        source_rules=_object(require_current={"const": True}, require_present={"const": True},
                             require_complete_context={"const": True}, allow_partial=known),
        thresholds=thresholds, budgets=budget, continuation_limits=counters,
        terminal_outcomes={"const": list(TERMINAL_OUTCOMES)},
        continuation_outcomes={"const": list(CONTINUATION_OUTCOMES)},
    )
    spent_fields = {
        "seconds": _nullable({"type": "number", "minimum": 0, "maximum": 1_000_000_000}),
        "model_calls": _nullable(_integer(1_000_000)), "tool_calls": _nullable(_integer(1_000_000)),
        "tokens": _nullable(_integer(1_000_000_000)), "cost_limit": _nullable(money),
        "claims": _nullable(_integer(1_000_000)), "sources": _nullable(_integer(1_000_000)),
        **{name: _nullable(_integer(1_000_000)) for name in _COUNTERS},
    }
    evaluator = _object(
        role=role, profile_ref=ident, controller_ref=ident, qualification_ref=_nullable(ident),
        qualified=flag, independent=flag, human_required=known, assurance=assurance,
        reference_cases=_nullable(_integer(1_000_000)), families=_nullable(_integer(10_000)),
        critical_false_acceptances=_nullable(_integer(1_000_000)),
        positive_acceptance=_nullable(ratio), meaning_preserving_flip=_nullable(ratio),
    )
    selected = _object(question_id=ident, outcome_ref=ident, basis_id=ident,
                       claim_refs={**_array(ident, 64, 1), "uniqueItems": True},
                       source_refs={**_array(_string(4096), 64, 1), "uniqueItems": True}, independently_eligible=flag,
                       **{name: flag for name in _FLAGS})
    observation = _object(
        schema_version={"const": OBSERVATION}, policy_id=policy_id, basis_id=ident, expected_basis_id=ident,
        observation_kind={"enum": ["canonical_owner", "agent_assertion"]}, owner_ref=ident, receipt_ref=ident,
        policy_compatible=flag, authority_granted=flag, model_required=known, model_granted=flag,
        acquisition_granted=flag, critical_disagreement=flag, transient_failure=flag,
        **{name: flag for name in _FLAGS},
        original_count=_integer(100, 1), accepted_count=_integer(100), unresolved_count=_integer(100),
        selected_outcomes=_array(selected, 100), evaluators=_array(evaluator, 8), spent=_object(**spent_fields),
        agent_rationale={"type": "string", "maxLength": 4096},
    )
    observation["required"].remove("agent_rationale")
    decision = _object(
        schema_version={"const": DECISION}, policy_id=policy_id,
        observation_sha256=_nullable({"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}),
        basis_id=_nullable(ident), outcome={"enum": [*TERMINAL_OUTCOMES, *CONTINUATION_OUTCOMES]},
        reason={"enum": list(_REASONS)}, terminal=known,
        next_effect=_nullable({"enum": list(EFFECTS)}), original_count=_integer(100, 1), eligible_count=_integer(100),
        unresolved_count=_integer(100), selected_outcome_refs=_array(ident, 100),
        authorized={"const": False}, evidence_accepted={"const": False},
    )
    policy_v2 = json.loads(json.dumps(policy))
    policy_v2["properties"]["schema_version"] = {"const": POLICY_V2}
    policy_v2["properties"]["scope"]["properties"]["source_scope_kind"] = {"enum": ["identity", "native_extent"]}
    policy_v2["properties"]["scope"]["required"].append("source_scope_kind")
    policy_v3 = json.loads(json.dumps(policy_v2))
    policy_v3["properties"]["schema_version"] = {"const": POLICY_V3}
    from .review_service_policy import schema_document as service_schema
    policy_v3["properties"]["review_services"] = service_schema()
    policy_v3["required"].append("review_services")
    observation_v2 = json.loads(json.dumps(observation))
    observation_v2["properties"]["schema_version"] = {"const": OBSERVATION_V2}
    observation_v2["properties"]["spent"]["properties"]["cost_limit"] = _nullable({"type": "string", "maxLength": 26,
        "pattern": r"^(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,12})?$(?![\s\S])"})
    observation_v2["properties"]["source_bindings"] = _array(
        _object(source_id=ident, extent_ref=_string(4096), closure_id=policy_id), 64)
    observation_v2["required"].append("source_bindings")
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
            for name, shape in ((POLICY, policy), (POLICY_V2, policy_v2), (POLICY_V3, policy_v3), (OBSERVATION, observation),
                                (OBSERVATION_V2, observation_v2), (DECISION, decision))}


@lru_cache(maxsize=1)
def _schema_bytes() -> tuple[tuple[str, bytes], ...]:
    """Retain only static schema declarations as immutable canonical bytes."""
    return tuple((name, json.dumps(shape, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()) for name, shape in _build_schemas().items())


def schemas() -> dict[str, dict[str, Any]]:
    """Return detached closed declarations; caller inputs are always validated."""
    return {name: json.loads(raw) for name, raw in _schema_bytes()}


def schema_document(name: str) -> dict[str, Any]:
    """Return a detached closed schema or a typed unsupported-version error."""
    if type(name) is str:
        for declared, raw in _schema_bytes():
            if declared == name:
                return json.loads(raw)
    _refuse("VERSION_UNSUPPORTED", "/schema_version")


def _bounded(value: Any) -> None:
    pending = [(value, 1, False)]
    ancestors: set[int] = set()
    nodes = string_bytes = 0
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            ancestors.remove(id(item))
            continue
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            _refuse("LIMIT")
        if type(item) in (dict, list):
            if id(item) in ancestors:
                _refuse("INVALID")
            if len(item) > MAX_NODES - nodes:
                _refuse("LIMIT")
            ancestors.add(id(item))
            pending.append((item, depth, True))
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    _refuse("INVALID")
                children = [child for pair in item.items() for child in pair]
            else:
                children = item
            pending.extend((child, depth + 1, False) for child in children)
        elif type(item) is str:
            if len(item) > MAX_BYTES:
                _refuse("LIMIT")
            try:
                string_bytes += len(item.encode("utf-8"))
            except UnicodeError:
                _refuse("INVALID")
            if string_bytes > MAX_BYTES:
                _refuse("LIMIT")
        elif type(item) is float:
            if not math.isfinite(item):
                _refuse("INVALID")
        elif type(item) is int:
            if item.bit_length() > 256:
                _refuse("LIMIT")
        elif item is not None and type(item) is not bool:
            _refuse("INVALID")


def _matches(value: Any, shape: dict[str, Any], path: str = "/") -> None:
    if "anyOf" in shape:
        for option in shape["anyOf"]:
            try:
                _matches(value, option, path)
                return
            except WorkflowContractError:
                continue
        _refuse("INVALID", path)
    types = {"object": (dict,), "array": (list,), "string": (str,), "integer": (int,),
             "number": (int, float), "boolean": (bool,), "null": (type(None),)}
    if "type" in shape and type(value) not in types[shape["type"]]:
        _refuse("INVALID", path)
    if "const" in shape and (type(value) is not type(shape["const"]) or value != shape["const"]):
        _refuse("INVALID", path)
    if "enum" in shape and value not in shape["enum"]:
        _refuse("INVALID", path)
    if type(value) is dict:
        properties = shape.get("properties", {})
        if value.keys() - properties.keys() or set(shape.get("required", ())) - value.keys():
            _refuse("INVALID", path)
        for key, child in value.items():
            _matches(child, properties[key], path.rstrip("/") + "/" + key)
    elif type(value) is list:
        if not shape.get("minItems", 0) <= len(value) <= shape.get("maxItems", MAX_NODES):
            _refuse("INVALID", path)
        if shape.get("uniqueItems") and any(child in value[:index] for index, child in enumerate(value)):
            _refuse("INVALID", path)
        for index, child in enumerate(value):
            _matches(child, shape.get("items", {}), path.rstrip("/") + f"/{index}")
    elif type(value) is str:
        if not shape.get("minLength", 0) <= len(value) <= shape.get("maxLength", MAX_BYTES):
            _refuse("LIMIT", path)
        if "pattern" in shape and re.search(shape["pattern"], value) is None:
            _refuse("INVALID", path)
    elif type(value) in (int, float):
        if value < shape.get("minimum", value) or value > shape.get("maximum", value):
            _refuse("INVALID", path)


def _canonical(value: Any) -> bytes:
    chunks = []
    size = 0
    for text in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).iterencode(value):
        raw = text.encode("utf-8")
        size += len(raw)
        if size > MAX_BYTES:
            _refuse("LIMIT")
        chunks.append(raw)
    return b"".join(chunks)


def _identity(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical({key: child for key, child in value.items() if key != "policy_id"})).hexdigest()


def validate_document(name: str, value: Any) -> dict[str, Any]:
    """Validate bounded shape and freeze policy identity; return detached data."""
    shape = schema_document(name)
    _bounded(value)
    if type(value) is dict and value.get("schema_version", name) != name:
        _refuse("VERSION_UNSUPPORTED", "/schema_version")
    _matches(value, shape)
    if name in {POLICY, POLICY_V2, POLICY_V3}:
        if len(value["scope"]["question_ids"]) > value["budgets"]["questions"]:
            _refuse("INVALID", "/scope/question_ids")
        thresholds = value["thresholds"]
        if Decimal(thresholds["positive_acceptance_min"]) < Decimal("0.90") or Decimal(thresholds["meaning_preserving_flip_max"]) > Decimal("0.05"):
            _refuse("INVALID", "/thresholds")
        required = value["required_evaluators"]
        keys = [(row["role"], row["profile_ref"]) for row in required]
        if len(keys) != len(set(keys)) or not any(row["role"] == "evaluator" for row in required):
            _refuse("INVALID", "/required_evaluators")
        if name in {POLICY_V2, POLICY_V3} and value["scope"]["source_scope_kind"] == "native_extent":
            from pathlib import Path

            if any(not Path(item).is_absolute() or ".." in Path(item).parts for item in value["scope"]["source_scope"]):
                _refuse("INVALID", "/scope/source_scope")
        if name == POLICY_V3:
            from .review_service_policy import ReviewServicePlan
            plan = ReviewServicePlan.restore(value["review_services"])
            slots = {row["profile_ref"]: row for row in required}
            if set(slots) != {route.slot_ref for route in plan.routes}:
                _refuse("INVALID", "/review_services")
            for route in plan.routes:
                if route.task_role != slots[route.slot_ref]["role"] or route.limit.controller_ref != slots[route.slot_ref]["controller_ref"]:
                    _refuse("INVALID", "/review_services")
                if any(getattr(route.limit, key) > value["budgets"][key] for key in ("seconds", "model_calls", "tool_calls", "tokens")) or Decimal(route.limit.cost_limit) > Decimal(value["budgets"]["cost_limit"]):
                    _refuse("INVALID", "/review_services")
        expected = _identity(value)
        if value["policy_id"] is not None and value["policy_id"] != expected:
            _refuse("IDENTITY_MISMATCH", "/policy_id")
        value = {**value, "policy_id": expected}
    return json.loads(_canonical(value))


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for key, child in pairs:
        if key in value:
            _refuse("INVALID")
        value[key] = child
    return value


def _constant(_value: str) -> NoReturn:
    _refuse("INVALID")


def decode_document(name: str, raw: bytes) -> dict[str, Any]:
    """Decode bounded UTF-8 JSON with no duplicate keys or nonfinite numbers."""
    schema_document(name)
    if type(raw) is not bytes or not raw:
        _refuse("INVALID")
    if len(raw) > MAX_BYTES:
        _refuse("LIMIT")
    try:
        text = raw.decode("utf-8")
        depth = 0
        quoted = escaped = False
        for character in text:
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth > MAX_DEPTH:
                    _refuse("LIMIT")
            elif character in "]}":
                depth -= 1
        value = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        _refuse("INVALID")
    return validate_document(name, value)


def encode_document(name: str, value: Any) -> bytes:
    """Encode a valid closed document as canonical JSON without altering originals."""
    return _canonical(validate_document(name, value))


@dataclass(frozen=True, init=False, slots=True)
class RunPolicy:
    """Host-created, immutable policy; nested views are detached plain dictionaries.

    Construct with an explicit full document. A null policy_id requests a newly
    computed identity; a supplied identity must match. Only canonical immutable
    bytes are retained. No caller mappings, environment or active global policy
    remain attached to this object.
    """

    _raw: bytes = field(repr=False)

    def __init__(self, document: Any) -> None:
        version = document.get("schema_version") if type(document) is dict else POLICY
        if type(version) is not str or version not in {POLICY, POLICY_V2, POLICY_V3}:
            _refuse("VERSION_UNSUPPORTED", "/schema_version")
        object.__setattr__(self, "_raw", encode_document(version, document))

    @classmethod
    def from_dict(cls, document: Any) -> RunPolicy:
        return cls(document)

    @classmethod
    def from_json(cls, raw: bytes) -> RunPolicy:
        if type(raw) is not bytes or not raw:
            _refuse("INVALID")
        if len(raw) > MAX_BYTES:
            _refuse("LIMIT")
        try:
            version = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant).get("schema_version")
        except (ValueError, UnicodeError, RecursionError, AttributeError):
            _refuse("INVALID")
        if type(version) is not str or version not in {POLICY, POLICY_V2, POLICY_V3}:
            _refuse("VERSION_UNSUPPORTED", "/schema_version")
        return cls(decode_document(version, raw))

    @property
    def policy_id(self) -> str:
        return self.to_dict()["policy_id"]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._raw)

    def to_json(self) -> bytes:
        return self._raw

    def decide(self, observed: Any) -> dict[str, Any]:
        return decide(self, observed)

    def evaluate(self, observed: Any) -> dict[str, Any]:
        return decide(self, observed)


def _profiles_qualified(policy: dict[str, Any], observed: dict[str, Any]) -> bool:
    profiles = observed["evaluators"]
    keys = [(row["role"], row["profile_ref"]) for row in profiles]
    required = policy["required_evaluators"]
    if len(keys) != len(set(keys)):
        return False
    lookup = dict(zip(keys, profiles, strict=True))
    if policy["schema_version"] == POLICY_V3:
        from .review_service_policy import ReviewServicePlan
        plan = ReviewServicePlan.restore(policy["review_services"])
        allowed = {(route.task_role, config) for route in plan.routes for config in route.config_refs}
        if set(keys) != allowed:
            return False
        selected = []
        for wanted in required:
            route = plan.route(wanted["profile_ref"])
            choices = [lookup[(wanted["role"], config)] for config in route.config_refs]
            good = [row for row in choices if row["qualified"] is True and row["independent"] is True]
            if not good:
                return False
            actual = good[0]
            selected.append((actual, {**wanted, "profile_ref": actual["profile_ref"], "controller_ref": actual["controller_ref"]}))
        if len({row[0]["controller_ref"] for row in selected}) != len(selected):
            return False
    else:
        if set(keys) != {(row["role"], row["profile_ref"]) for row in required}:
            return False
        selected = [(lookup[(wanted["role"], wanted["profile_ref"])], wanted) for wanted in required]
    thresholds = policy["thresholds"]
    for row, wanted in selected:
        if (row["qualified"] is not True or row["independent"] is not True or row["qualification_ref"] is None
                or row["controller_ref"] != wanted["controller_ref"] or row["assurance"] != policy["assurance"]):
            return False
        metrics = ("reference_cases", "families", "critical_false_acceptances", "positive_acceptance", "meaning_preserving_flip")
        if any(row[name] is None for name in metrics):
            return False
        if (row["reference_cases"] < thresholds["min_reference_cases"] or row["families"] < thresholds["min_families"]
                or row["critical_false_acceptances"] > thresholds["critical_false_acceptances_max"]
                or Decimal(row["positive_acceptance"]) < Decimal(thresholds["positive_acceptance_min"])
                or Decimal(row["meaning_preserving_flip"]) > Decimal(thresholds["meaning_preserving_flip_max"])):
            return False
    return True


def _owner_observation(frozen, observed):
    """Validate the existing closed owner shape; this does not authenticate it."""
    try:
        version = OBSERVATION_V2 if frozen["schema_version"] in {POLICY_V2, POLICY_V3} else OBSERVATION
        return validate_document(version, observed)
    except WorkflowContractError:
        return None


def _hard_policy_failure(frozen, owner):
    """Shared hard binding, scope, authority and exhaustion predicates.

    Native owners authenticate observations before this pure predicate. Derived
    subsets retain the original observation and original-level counts.
    """
    if owner is None:
        return ("rejected", "invalid_input")
    count = len(frozen["scope"]["question_ids"])
    def permits(effect):
        return effect in frozen["allowed_effects"] and frozen["host_capability_refs"].get(effect) is not None

    if owner["policy_id"] != frozen["policy_id"]:
        return ("rejected", "policy_binding")
    if owner["observation_kind"] != "canonical_owner":
        return ("rejected", "untrusted_observation")
    selected = owner["selected_outcomes"]
    ids = [row["question_id"] for row in selected]
    refs = [row["outcome_ref"] for row in selected]
    authorized_sources = set(frozen["scope"]["source_scope"])
    if frozen["scope"].get("source_scope_kind") == "native_extent":
        bindings = owner["source_bindings"]
        if (len({row["source_id"] for row in bindings}) != len(bindings)
                or any(row["extent_ref"] not in authorized_sources for row in bindings)):
            return ("rejected", "scope_accounting")
        authorized_sources = {row["source_id"] for row in bindings}
    if (owner["original_count"] != count or owner["accepted_count"] + owner["unresolved_count"] != count
            or owner["accepted_count"] != len(selected) or len(ids) != len(set(ids)) or len(refs) != len(set(refs))
            or set(ids) - set(frozen["scope"]["question_ids"])
            or any(set(row["source_refs"]) - authorized_sources for row in selected)):
        return ("rejected", "scope_accounting")
    if (frozen["human_review"] or any(row["human_required"] for row in frozen["required_evaluators"])
            or any(row["human_required"] for row in owner["evaluators"]) or owner["policy_compatible"] is not True):
        return ("policy_incompatible", "human_review_required")
    if owner["authority_granted"] is not True:
        return ("blocked_external", "authority_unavailable")
    if owner["model_required"] and (owner["model_granted"] is not True or not permits("model")):
        return ("blocked_external", "executor_unavailable")
    spent = owner["spent"]
    budgets = frozen["budgets"]
    if any(value is None for value in spent.values()):
        return ("stop_budget", "usage_unknown")
    selected_sources = {reference for row in selected for reference in row["source_refs"]}
    # A claim may support several selected questions. Bound its actual unique
    # reference extent independently of the owner's observed usage counter.
    selected_claims = {reference for row in selected for reference in row["claim_refs"]}
    if (spent["seconds"] >= budgets["seconds"] or spent["tool_calls"] > budgets["tool_calls"]
            or (budgets["tool_calls"] > 0 and spent["tool_calls"] == budgets["tool_calls"])
            or Decimal(spent["cost_limit"]) > Decimal(budgets["cost_limit"])
            or len(selected_sources) > budgets["sources"]
            or len(selected_claims) > budgets["claims"]
            or any(spent[name] > budgets[name] for name in ("model_calls", "tokens", "claims", "sources"))
            or any(spent[name] > frozen["continuation_limits"][name] for name in _COUNTERS)):
        return ("stop_budget", "budget_exhausted")
    if owner["model_required"] and (spent["model_calls"] >= budgets["model_calls"] or spent["tokens"] >= budgets["tokens"]):
        return ("stop_budget", "model_budget_exhausted")
    return None


def _derived_scope_failure(frozen, owner, *, claim_refs, source_refs):
    """Bound native-derived extent without minting an accepted original row."""
    allowed = set(frozen["scope"]["source_scope"])
    if frozen["scope"].get("source_scope_kind") == "native_extent":
        allowed = {row["source_id"] for row in owner["source_bindings"]}
    if not set(source_refs) <= allowed:
        return "scope_accounting"
    if len(set(source_refs)) > frozen["budgets"]["sources"] or len(set(claim_refs)) > frozen["budgets"]["claims"]:
        return "budget_exhausted"
    return None


def decide(policy: RunPolicy, observed: Any) -> dict[str, Any]:
    """Return a deterministic inert record from authenticated owner observations.

    Malformed or missing observations produce a terminal rejection. Narrative
    rationale is bounded but excluded from the decision basis. Unknown measured
    usage stops further work, rather than being interpreted as zero. This gate
    neither authenticates receipt strings nor reserves future execution costs.
    """
    if type(policy) is not RunPolicy:
        _refuse("INVALID", "/policy")
    frozen = policy.to_dict()
    count = len(frozen["scope"]["question_ids"])
    owner = _owner_observation(frozen, observed)
    evidence = {key: value for key, value in owner.items() if key != "agent_rationale"} if owner else None

    def record(outcome: str, reason: str, effect: str | None = None, eligible: bool = False) -> dict[str, Any]:
        selected = owner["selected_outcomes"] if eligible and owner else []
        result = {
            "schema_version": DECISION, "policy_id": frozen["policy_id"],
            "observation_sha256": hashlib.sha256(_canonical(evidence)).hexdigest() if evidence else None,
            "basis_id": owner["basis_id"] if owner else None, "outcome": outcome, "reason": reason,
            "terminal": outcome in TERMINAL_OUTCOMES, "next_effect": effect,
            "original_count": count, "eligible_count": len(selected), "unresolved_count": count - len(selected),
            "selected_outcome_refs": [row["outcome_ref"] for row in selected],
            "authorized": False, "evidence_accepted": False,
        }
        return validate_document(DECISION, result)

    def permits(effect: str) -> bool:
        return effect in frozen["allowed_effects"] and frozen["host_capability_refs"].get(effect) is not None

    def continuation(outcome: str, reason: str, counter: str, fallback: str, effect: str) -> dict[str, Any]:
        if owner["spent"][counter] >= frozen["continuation_limits"][counter]:
            return record(fallback, reason)
        if not permits(effect):
            return record("blocked_external", "effect_ungranted")
        return record(outcome, reason, effect)

    if owner is None:
        return record("rejected", "invalid_input")
    failure = _hard_policy_failure(frozen, owner)
    if failure is not None:
        return record(*failure)
    selected = owner["selected_outcomes"]
    stale = (owner["basis_id"] != owner["expected_basis_id"] or owner["source_current"] is not True
             or any(row["basis_id"] != owner["basis_id"] or row["source_current"] is not True for row in selected))
    if stale:
        return continuation("revise", "basis_changed", "repair_cycles", "abstained", "revise")
    if (owner["qualified"] is not True or any(row["qualified"] is not True for row in selected)
            or not _profiles_qualified(frozen, owner)):
        return record("blocked_external", "qualification_unknown")
    if owner["mechanical_pass"] is not True or any(row["mechanical_pass"] is not True for row in selected):
        return continuation("revise", "mechanical_failure", "repair_cycles", "rejected", "revise")
    if owner["critical_disagreement"] is not False:
        return continuation("revise", "review_disagreement", "adjudication_cycles", "abstained", "adjudicate")
    if owner["review_pass"] is not True or any(row["review_pass"] is not True or row["independently_eligible"] is not True for row in selected):
        return continuation("revise", "unsupported_claim", "repair_cycles", "abstained", "revise")
    if (owner["source_present"] is not True or owner["context_complete"] is not True
            or any(row["source_present"] is not True or row["context_complete"] is not True for row in selected)):
        if owner["acquisition_granted"] is not True or not permits("acquire"):
            return record("abstained", "acquisition_ungranted")
        return continuation("acquire_more", "insufficient_evidence", "acquisition_attempts", "abstained", "acquire")
    if owner["transient_failure"] is not False:
        return continuation("retry", "executor_unavailable", "retry_attempts", "blocked_external", "retry")
    if not selected:
        return record("abstained", "current_outcomes")
    if owner["unresolved_count"]:
        if not frozen["source_rules"]["allow_partial"]:
            return record("abstained", "partial_disallowed")
        return record("partial", "current_outcomes", eligible=True)
    return record("accepted", "current_outcomes", eligible=True)


def evaluate(policy: RunPolicy, observed: Any) -> dict[str, Any]:
    """Evaluate the frozen policy without changing policy or owner state."""
    return decide(policy, observed)
