"""Pure accounting reducers for a canonical owner's retained execution ledger.

The host authenticates complete observations through an explicit callback.
These reducers own no storage and dispatch no work. Callers must retain their
outputs in the canonical protected run journal; decoding caller JSON does not
authenticate a ledger or authorize spending.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .mutation_contracts import _shape
from .workflow_contracts import WorkflowContractError

PLAN = "execution-coverage-plan/v1"
OBSERVATION = "execution-accounting-observation/v1"
CLOSURE = "execution-coverage-closure/v1"
VERIFICATION = "execution-accounting-verification/v1"
REPORT = "execution-accounting-report/v2"
MAX_ATTEMPTS = 128
MAX_OBSERVATIONS = 1024
MAX_BYTES = 4 * 1024 * 1024
USAGE_FIELDS = ("seconds", "model_calls", "tool_calls", "tokens", "cost")
COUNT_FIELDS = ("model_calls", "tool_calls", "tokens")
MAX_COST_INTEGER_DIGITS = 15
COST_DECIMAL_PLACES = 12
SECONDS_DECIMAL_PLACES = 9
MAX_AGGREGATE_INTEGER_DIGITS = 24
_UTC = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$(?![\s\S])"


def refuse(reason):
    raise WorkflowContractError("ACCOUNTING_" + reason, "Execution accounting refused the input.",
        recoverable=False, details={"reason": reason},
        remediation="Preserve the predeclared ledger and authenticate exact host observations.") from None


def _object(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(maximum=256):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _nullable(shape):
    return {"anyOf": [shape, {"type": "null"}]}


def _array(shape, maximum=64):
    return {"type": "array", "items": shape, "maxItems": maximum}


def _shapes():
    text = _text()
    utc = {**_text(32), "pattern": _UTC}
    decimal = {**_text(MAX_COST_INTEGER_DIGITS + SECONDS_DECIMAL_PLACES + 1),
        "pattern": rf"^(?:0|[1-9][0-9]{{0,{MAX_COST_INTEGER_DIGITS - 1}}})(?:\.[0-9]{{1,{SECONDS_DECIMAL_PLACES}}})?$(?![\s\S])"}
    money = {**_text(MAX_COST_INTEGER_DIGITS + COST_DECIMAL_PLACES + 1),
        "pattern": rf"^(?:0|[1-9][0-9]{{0,{MAX_COST_INTEGER_DIGITS - 1}}})(?:\.[0-9]{{1,{COST_DECIMAL_PLACES}}})?$(?![\s\S])"}
    aggregate_decimal = {**_text(MAX_AGGREGATE_INTEGER_DIGITS + SECONDS_DECIMAL_PLACES + 1),
        "pattern": rf"^(?:0|[1-9][0-9]{{0,{MAX_AGGREGATE_INTEGER_DIGITS - 1}}})(?:\.[0-9]{{1,{SECONDS_DECIMAL_PLACES}}})?$(?![\s\S])"}
    aggregate_money = {**_text(MAX_AGGREGATE_INTEGER_DIGITS + COST_DECIMAL_PLACES + 1),
        "pattern": rf"^(?:0|[1-9][0-9]{{0,{MAX_AGGREGATE_INTEGER_DIGITS - 1}}})(?:\.[0-9]{{1,{COST_DECIMAL_PLACES}}})?$(?![\s\S])"}
    count = {"type": "integer", "minimum": 0, "maximum": 10_000_000_000}
    boolean = {"type": "boolean"}
    hashed = {**_text(71), "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    binding = {"type": "object", "minProperties": 1, "maxProperties": 32, "additionalProperties": True}
    usage = _object(**{key: _nullable(count if key in COUNT_FIELDS else money if key == "cost" else decimal)
        for key in USAGE_FIELDS})
    unit = _nullable(_object(currency={**_text(3), "pattern": r"^[A-Z]{3}$(?![\s\S])"}, pricing_ref=text))
    observer = _nullable(_object(observer_id=text, scope_id=text, started_at=utc, observed_through_at=utc,
        human_interventions=_nullable(count), gaps=_array(text), exclusions=_array(text)))
    observation = _object(schema_version={"const": OBSERVATION}, observation_id=text, binding=binding,
        scope_id=text, controller_ref=text, attempt_id=_nullable(text), observed_at=utc, sequence=count,
        slots=_array(text, 16), state={"enum": ["started", "completed", "failed", "cancelled", "unknown"]},
        usage=usage, cost_unit=unit, provenance={"enum": ["measured", "imported", "estimated", "unavailable"]},
        final_usage=boolean, correction=boolean, artifact_refs=_array(hashed, 32), human_observer=observer,
        authentication=_nullable(_text(4096)))
    verification = _object(schema_version={"const": VERIFICATION}, envelope_sha256=hashed, binding=binding,
        controller_ref=text, verified_at=utc, expires_at=utc, authenticated={"const": True})
    retained = _object(envelope=observation, verification=verification)
    attempt = _object(attempt_id=text, job_id=text, controller_ref=text, issued_at=utc,
        reservation=usage, cost_unit=unit, required_slots=_array(text, 16))
    slot = _object(slot_id=text, controller_ref=text, declared_at=utc)
    closure = _object(schema_version={"const": CLOSURE}, binding=binding, scope_id=text, controller_ref=text,
        plan_sha256=hashed, closed_at=utc, terminal_decision_ref=text,
        last_observation_ids=_array(text, MAX_OBSERVATIONS), authentication=_nullable(_text(4096)))
    plan = _object(schema_version={"const": PLAN}, binding=binding, scope_id=text, opened_at=utc,
        revision=count, slots=_array(slot, MAX_OBSERVATIONS), attempts=_array(attempt, MAX_ATTEMPTS),
        observations=_array(retained, MAX_OBSERVATIONS),
        closure=_nullable(_object(envelope=closure, verification=verification)))
    aggregate_usage = _object(**{key: _nullable({"type": "integer", "minimum": 0, "maximum": 10_000_000_000_000}
        if key in COUNT_FIELDS else aggregate_money if key == "cost" else aggregate_decimal) for key in USAGE_FIELDS})
    ids = _array(text, MAX_OBSERVATIONS)
    coverage = _object(scope_id=text, revision=count, spent=aggregate_usage, outstanding=aggregate_usage,
        committed=aggregate_usage, usage=aggregate_usage, usage_unknown=boolean, expected_attempt_ids=ids,
        observed_attempt_ids=ids, missing_attempt_ids=ids, incomplete_attempt_ids=ids, missing_slots=ids,
        missing_sequence_ranges=_array(_object(attempt_id=text, first_sequence=count, last_sequence=count), MAX_OBSERVATIONS),
        conflicting_attempt_ids=ids, coverage_closed=boolean, coverage_complete=boolean, cost_unit=unit,
        cost_subtotals=_array(_object(cost_unit=unit, known_subtotal=aggregate_money), MAX_OBSERVATIONS))
    report_shape = _object(schema_version={"const": REPORT}, binding=binding, scope_id=text, plan_sha256=hashed,
        coverage=coverage, totals=aggregate_usage, observed_human_interventions=_nullable(count),
        limitations=_array(text), authorized={"const": False}, evidence_accepted={"const": False})
    return {PLAN: plan, OBSERVATION: observation, CLOSURE: closure, VERIFICATION: verification, REPORT: report_shape,
        "attempt": attempt, "slot": slot, "usage": usage}


def schema_document(name):
    """Return a detached closed codec, without conferring observation authority."""
    if name not in _shapes():
        refuse("VERSION_UNSUPPORTED")
    return _clone({"$schema": "https://json-schema.org/draft/2020-12/schema", **_shapes()[name]})


def _clone(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 100_000 or depth > 16:
            refuse("LIMIT")
        if type(item) is dict:
            if len(item) > 128 or any(type(key) is not str for key in item):
                refuse("INVALID")
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
        elif type(item) is list:
            if len(item) > MAX_OBSERVATIONS:
                refuse("LIMIT")
            pending.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            if len(item) > MAX_BYTES:
                refuse("LIMIT")
        elif type(item) is int:
            if item.bit_length() > 128:
                refuse("LIMIT")
        elif item is not None and type(item) is not bool:
            refuse("INVALID")
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > MAX_BYTES:
            refuse("LIMIT")
        return json.loads(raw)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        refuse("INVALID")


def digest(value):
    """Identity for an exact bounded envelope; identity alone is not authentication."""
    raw = json.dumps(_clone(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _time(value):
    if type(value) is not str or re.fullmatch(_UTC, value) is None:
        refuse("CLOCK_INVALID")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refuse("CLOCK_INVALID")


def _unique(values):
    return len(values) == len(set(values))


def validate_document(name, value):
    """Decode inert data; callers must use trusted intake before retaining it."""
    if name not in _shapes():
        refuse("VERSION_UNSUPPORTED")
    value = _clone(value)
    try:
        _shape(value, _shapes()[name])
    except (Exception, SystemExit):
        refuse("INVALID")
    if name == OBSERVATION:
        _time(value["observed_at"])
        if value["attempt_id"] is not None and value["sequence"] == 0 and value["state"] != "unknown":
            refuse("SEQUENCE_INVALID")
        if not value["slots"] or not _unique(value["slots"]):
            refuse("SLOT_INVALID")
        if value["final_usage"] and value["state"] not in {"completed", "failed", "cancelled"}:
            refuse("FINAL_INVALID")
        if value["correction"] and not value["final_usage"]:
            refuse("CORRECTION_INVALID")
        if value["provenance"] == "unavailable" and any(row is not None for row in value["usage"].values()):
            refuse("PROVENANCE_INVALID")
        observer = value["human_observer"]
        if observer is not None:
            if _time(observer["observed_through_at"]) < _time(observer["started_at"]):
                refuse("OBSERVER_INVALID")
            if observer["scope_id"] != value["scope_id"]:
                refuse("OBSERVER_INVALID")
    elif name == VERIFICATION:
        if _time(value["expires_at"]) <= _time(value["verified_at"]):
            refuse("VERIFICATION_INVALID")
    elif name == CLOSURE:
        _time(value["closed_at"])
        if not _unique(value["last_observation_ids"]):
            refuse("CLOSURE_INVALID")
    elif name == "attempt":
        _time(value["issued_at"])
        if not value["required_slots"] or not _unique(value["required_slots"]) or "usage" not in value["required_slots"]:
            refuse("SLOT_INVALID")
        if any(item is None for item in value["reservation"].values()):
            refuse("RESERVATION_UNKNOWN")
        if value["cost_unit"] is None:
            refuse("COST_UNIT_REQUIRED")
    elif name == "slot":
        _time(value["declared_at"])
    elif name == PLAN:
        _time(value["opened_at"])
        if not value["observations"]:
            refuse("BASELINE_UNKNOWN")
        initial = value["observations"][0]["envelope"]
        if (initial["attempt_id"] is not None or initial["slots"] != ["baseline"] or initial["sequence"] != 0
                or initial["observed_at"] != value["opened_at"] or initial["provenance"] != "measured"
                or initial["cost_unit"] is None or any(item not in (0, "0") for item in initial["usage"].values())):
            refuse("BASELINE_INVALID")
        if not _unique([item["attempt_id"] for item in value["attempts"]]):
            refuse("ATTEMPT_CONFLICT")
        if not _unique([item["slot_id"] for item in value["slots"]]):
            refuse("SLOT_CONFLICT")
        if len(value["slots"]) + sum(len(item["required_slots"]) for item in value["attempts"]) > MAX_OBSERVATIONS:
            refuse("LIMIT")
        if not _unique([item["envelope"]["observation_id"] for item in value["observations"]]):
            refuse("OBSERVATION_CONFLICT")
        if not _unique([(item["envelope"]["attempt_id"], item["envelope"]["controller_ref"], item["envelope"]["sequence"])
                for item in value["observations"]]):
            refuse("SEQUENCE_CONFLICT")
        for attempt in value["attempts"]:
            validate_document("attempt", attempt)
        for row in value["observations"]:
            validate_document(OBSERVATION, row["envelope"])
            validate_document(VERIFICATION, row["verification"])
            if row["verification"]["envelope_sha256"] != digest(row["envelope"]):
                refuse("VERIFICATION_CONFLICT")
            if (row["verification"]["binding"] != row["envelope"]["binding"]
                    or row["verification"]["controller_ref"] != row["envelope"]["controller_ref"]):
                refuse("VERIFICATION_CONFLICT")
            if row["envelope"]["binding"] != value["binding"] or row["envelope"]["scope_id"] != value["scope_id"]:
                refuse("BINDING_CONFLICT")
        if value["closure"] is not None:
            validate_document(CLOSURE, value["closure"]["envelope"])
            validate_document(VERIFICATION, value["closure"]["verification"])
            close = value["closure"]
            if (close["verification"]["envelope_sha256"] != digest(close["envelope"])
                    or close["verification"]["binding"] != close["envelope"]["binding"]
                    or close["verification"]["controller_ref"] != close["envelope"]["controller_ref"]
                    or close["envelope"]["binding"] != value["binding"]
                    or close["envelope"]["scope_id"] != value["scope_id"]):
                refuse("VERIFICATION_CONFLICT")
            preclose = _clone(value)
            preclose["closure"] = None
            retained_ids = set(close["envelope"]["last_observation_ids"])
            preclose["observations"] = [row for row in preclose["observations"] if row["envelope"]["observation_id"] in retained_ids]
            if digest(preclose) != close["envelope"]["plan_sha256"]:
                refuse("CLOSURE_CONFLICT")
    elif name == REPORT:
        if not value["coverage"]["coverage_complete"] and any(row is not None for row in value["totals"].values()):
            refuse("TOTAL_UNOBSERVED")
    return value


@dataclass(frozen=True)
class CoverageVerifier:
    """Trusted finite host callback for exact envelope verification.

    Its controller reference is a correlation value, not proof of a principal.
    Hosts must bound verification and check the protected measurement producer.
    """

    controller_ref: str
    verify: object = field(repr=False, compare=False)

    def __post_init__(self):
        if type(self.controller_ref) is not str or not 1 <= len(self.controller_ref) <= 256 or not callable(self.verify):
            refuse("VERIFIER_INVALID")


def _authenticate(envelope, verifier, *, now_utc=None):
    if not isinstance(verifier, CoverageVerifier) or verifier.controller_ref != envelope["controller_ref"]:
        refuse("VERIFIER_REQUIRED")
    request = {"envelope_sha256": digest(envelope), "envelope": _clone(envelope), "binding": envelope["binding"]}
    try:
        verified = validate_document(VERIFICATION, verifier.verify(_clone(request)))
    except (Exception, SystemExit):
        refuse("OBSERVATION_UNAUTHENTICATED")
    now = datetime.now(timezone.utc) if now_utc is None else _time(now_utc)
    if (verified["envelope_sha256"] != request["envelope_sha256"] or verified["binding"] != envelope["binding"]
            or verified["controller_ref"] != envelope["controller_ref"]
            or not _time(verified["verified_at"]) <= now < _time(verified["expires_at"])):
        refuse("OBSERVATION_UNAUTHENTICATED")
    return {"envelope": envelope, "verification": verified}


def authenticate_plan(plan, verifiers, *, now_utc=None):
    """Reauthenticate retained signed envelopes after loading canonical storage.

    A passed JSON boolean or a prior verification certificate is insufficient.
    The host callback must check persistent producer authentication, including
    exact external receipts where an accounting wrapper references them.
    """
    plan = validate_document(PLAN, plan)
    if type(verifiers) is not dict:
        refuse("VERIFIER_REQUIRED")
    rows = plan["observations"] + ([] if plan["closure"] is None else [plan["closure"]])
    for row in rows:
        _authenticate(row["envelope"], verifiers.get(row["envelope"]["controller_ref"]), now_utc=now_utc)
    return plan


def start_coverage(*, binding, scope_id, baseline, verifier, now_utc=None):
    """Open a measured scope before its work; an empty ledger cannot imply zero."""
    observation = validate_document(OBSERVATION, baseline)
    if (observation["binding"] != binding or observation["scope_id"] != scope_id or observation["attempt_id"] is not None
            or observation["slots"] != ["baseline"] or observation["sequence"] != 0 or observation["correction"]):
        refuse("BASELINE_INVALID")
    if observation["provenance"] != "measured" or observation["cost_unit"] is None:
        refuse("BASELINE_UNKNOWN")
    if any(value not in (0, "0") for value in observation["usage"].values()):
        refuse("BASELINE_UNKNOWN")
    retained = _authenticate(observation, verifier, now_utc=now_utc)
    return validate_document(PLAN, {"schema_version": PLAN, "binding": _clone(binding), "scope_id": scope_id,
        "opened_at": observation["observed_at"], "revision": 0,
        "slots": [{"slot_id": "baseline", "controller_ref": observation["controller_ref"],
            "declared_at": observation["observed_at"]}], "attempts": [], "observations": [retained], "closure": None})


def declare_slot(plan, *, slot_id, controller_ref, declared_at):
    """Declare a non-attempt decision/observer slot before its work occurs."""
    plan = validate_document(PLAN, plan)
    slot = validate_document("slot", {"slot_id": slot_id, "controller_ref": controller_ref, "declared_at": declared_at})
    if plan["closure"] is not None:
        refuse("COVERAGE_CLOSED")
    existing = next((item for item in plan["slots"] if item["slot_id"] == slot_id), None)
    if existing is not None:
        if existing != slot:
            refuse("SLOT_CONFLICT")
        return plan
    if _time(declared_at) < _time(plan["opened_at"]):
        refuse("CLOCK_REGRESSION")
    plan["slots"].append(slot)
    plan["revision"] += 1
    return validate_document(PLAN, plan)


def reserve_attempt(plan, attempt):
    """Append an immutable pre-dispatch inventory and maximum reservation."""
    plan = validate_document(PLAN, plan)
    attempt = validate_document("attempt", attempt)
    existing = next((item for item in plan["attempts"] if item["attempt_id"] == attempt["attempt_id"]), None)
    if existing is not None:
        if existing != attempt:
            refuse("ATTEMPT_CONFLICT")
        return plan
    if plan["closure"] is not None:
        refuse("COVERAGE_CLOSED")
    if _time(attempt["issued_at"]) < _time(plan["opened_at"]):
        refuse("CLOCK_REGRESSION")
    plan["attempts"].append(attempt)
    plan["revision"] += 1
    return validate_document(PLAN, plan)


def observe_attempt(plan, observation, verifier, *, now_utc=None):
    """Retain a correlated authenticated observation, including historical usage.

    Exact repeats are idempotent. A previously missing logical sequence may
    arrive late; retained arrival order remains unchanged. Contradictory final
    receipts make the projection uncertain and never rewrite an earlier receipt.
    """
    plan = validate_document(PLAN, plan)
    observation = validate_document(OBSERVATION, observation)
    if observation["binding"] != plan["binding"] or observation["scope_id"] != plan["scope_id"]:
        refuse("BINDING_CONFLICT")
    existing = next((row["envelope"] for row in plan["observations"]
        if row["envelope"]["observation_id"] == observation["observation_id"]), None)
    if existing is not None:
        if existing != observation:
            refuse("OBSERVATION_CONFLICT")
        return plan
    attempt_id = observation["attempt_id"]
    if attempt_id is None:
        if any(value not in (None, 0, "0") for value in observation["usage"].values()):
            refuse("UNSCOPED_USAGE")
        declared = {row["slot_id"]: row for row in plan["slots"]}
        if any(slot not in declared or declared[slot]["controller_ref"] != observation["controller_ref"]
                or _time(observation["observed_at"]) < _time(declared[slot]["declared_at"])
                for slot in observation["slots"]):
            refuse("SLOT_UNDECLARED")
    else:
        attempt = next((row for row in plan["attempts"] if row["attempt_id"] == attempt_id), None)
        if (attempt is None or observation["controller_ref"] != attempt["controller_ref"]
                or not set(observation["slots"]) <= set(attempt["required_slots"])
                or _time(observation["observed_at"]) < _time(attempt["issued_at"])):
            refuse("ATTEMPT_UNDECLARED")
    earlier = [row["envelope"] for row in plan["observations"]
        if row["envelope"]["attempt_id"] == attempt_id and row["envelope"]["controller_ref"] == observation["controller_ref"]]
    if any(row["sequence"] == observation["sequence"] for row in earlier):
        refuse("SEQUENCE_CONFLICT")
    plan["observations"].append(_authenticate(observation, verifier, now_utc=now_utc))
    return validate_document(PLAN, plan)


def close_coverage(plan, closure, verifier, *, now_utc=None):
    """Freeze the independently declared inventory at a terminal decision."""
    plan = validate_document(PLAN, plan)
    closure = validate_document(CLOSURE, closure)
    if plan["closure"] is not None:
        if plan["closure"]["envelope"] != closure:
            refuse("CLOSURE_CONFLICT")
        return plan
    if (closure["binding"] != plan["binding"] or closure["scope_id"] != plan["scope_id"]
            or closure["plan_sha256"] != digest(plan)
            or sorted(closure["last_observation_ids"]) != sorted(row["envelope"]["observation_id"] for row in plan["observations"])
            or _time(closure["closed_at"]) < max(_time(row["envelope"]["observed_at"]) for row in plan["observations"])):
        refuse("CLOSURE_CONFLICT")
    plan["closure"] = _authenticate(closure, verifier, now_utc=now_utc)
    return validate_document(PLAN, plan)


def _integer(value):
    whole, _, fraction = value.partition(".")
    return int(whole + fraction), len(fraction)


def _decimal(value, places):
    if places == 0:
        return str(value)
    digits = str(value).zfill(places + 1)
    fraction = digits[-places:].rstrip("0")
    return digits[:-places] + ("." + fraction if fraction else "")


def _sum(values):
    numbers = [_integer(value) for value in values]
    places = max((scale for _value, scale in numbers), default=0)
    return _decimal(sum(value * 10 ** (places - scale) for value, scale in numbers), places)


def _compare(left, right):
    a, sa = _integer(left)
    b, sb = _integer(right)
    scale = max(sa, sb)
    return (a * 10 ** (scale - sa) > b * 10 ** (scale - sb)) - (a * 10 ** (scale - sa) < b * 10 ** (scale - sb))


def _difference(limit, spent):
    a, sa = _integer(limit)
    b, sb = _integer(spent)
    scale = max(sa, sb)
    return _decimal(max(0, a * 10 ** (scale - sa) - b * 10 ** (scale - sb)), scale)


def _maximum(values):
    result = "0"
    for value in values:
        if _compare(value, result) > 0:
            result = value
    return result


def _sequence_gaps(attempt_id, rows):
    """Bound missing sequences as inclusive ranges, including a lost first event."""
    sequences = sorted({row["sequence"] for row in rows})
    expected = 0 if sequences and sequences[0] == 0 else 1
    gaps = []
    for sequence in sequences:
        if sequence > expected:
            gaps.append({"attempt_id": attempt_id, "first_sequence": expected, "last_sequence": sequence - 1})
        expected = sequence + 1
    return gaps


def projection(plan, *, limits=None):
    """Project a measured prefix and outstanding maxima before final closure.

    ``spent`` contains confirmed known consumption. ``usage`` remains unknown
    when any attempted measurement is unresolved. ``committed`` conservatively
    sums spent plus occupied maximum reservations, including uncertain work.
    """
    plan = validate_document(PLAN, plan)
    baseline = plan["observations"][0]["envelope"]
    spent = {key: [] for key in USAGE_FIELDS}
    occupied = {key: [] for key in USAGE_FIELDS}
    observed, incomplete, missing_slots, missing_sequences, conflicts, cost_groups = [], [], [], [], [], {}
    unresolved = False
    for attempt in plan["attempts"]:
        rows = sorted((row["envelope"] for row in plan["observations"]
            if row["envelope"]["attempt_id"] == attempt["attempt_id"]), key=lambda row: row["sequence"])
        gaps = _sequence_gaps(attempt["attempt_id"], rows)
        missing_sequences.extend(gaps)
        if rows:
            observed.append(attempt["attempt_id"])
        covered = {slot for row in rows if row["provenance"] == "measured" for slot in row["slots"]}
        missing_slots.extend(attempt["attempt_id"] + ":" + slot for slot in attempt["required_slots"] if slot not in covered)
        meters = [row for row in rows if row["provenance"] == "measured" and "usage" in row["slots"]]
        finals = [row for row in meters if row["final_usage"]]
        final = finals[-1] if finals else None
        conflict = bool(finals and any(row["usage"] != final["usage"] or row["cost_unit"] != final["cost_unit"] for row in finals))
        for previous, current in zip(rows, rows[1:], strict=False):
            if (_time(current["observed_at"]) < _time(previous["observed_at"])
                    or previous["state"] in {"completed", "failed", "cancelled"} and current["state"] != previous["state"]):
                conflict = True
        for previous, current in zip(meters, meters[1:], strict=False):
            for key in USAGE_FIELDS:
                left, right = previous["usage"][key], current["usage"][key]
                if (left is not None and right is not None and (key != "cost" or previous["cost_unit"] == current["cost_unit"])
                        and _compare(str(right), str(left)) < 0):
                    conflict = True
        if conflict:
            conflicts.append(attempt["attempt_id"])
        compatible = final is not None and final["cost_unit"] == attempt["cost_unit"] == baseline["cost_unit"]
        known_final = final is not None and all(value is not None for value in final["usage"].values())
        settled = known_final and compatible and not conflict and not gaps
        if not settled:
            incomplete.append(attempt["attempt_id"])
            unresolved = True
        for key in USAGE_FIELDS:
            values = [str(row["usage"][key]) for row in meters if row["usage"][key] is not None
                and (key != "cost" or row["cost_unit"] == baseline["cost_unit"])]
            amount = _maximum(values)
            spent[key].append(amount)
            if not settled:
                occupied[key].append(str(attempt["reservation"][key]))
        for unit in {digest(row["cost_unit"]) for row in meters if row["cost_unit"] is not None}:
            group_rows = [row for row in meters if digest(row["cost_unit"]) == unit and row["usage"]["cost"] is not None]
            if group_rows:
                amount = _maximum(row["usage"]["cost"] for row in group_rows)
                cost_groups.setdefault(unit, {"cost_unit": group_rows[0]["cost_unit"], "values": []})["values"].append(amount)
    global_covered = {slot for row in plan["observations"] if row["envelope"]["attempt_id"] is None
        and row["envelope"]["provenance"] == "measured" for slot in row["envelope"]["slots"]}
    missing_slots.extend(row["slot_id"] for row in plan["slots"] if row["slot_id"] not in global_covered)
    spent_totals = {key: _sum(values) for key, values in spent.items()}
    occupied_totals = {key: _sum(values) for key, values in occupied.items()}
    committed = {key: _sum([spent_totals[key], occupied_totals[key]]) for key in USAGE_FIELDS}
    for values in (spent_totals, occupied_totals, committed):
        for key in COUNT_FIELDS:
            values[key] = int(values[key])
    unknown = unresolved or bool(conflicts)
    closed_ids = set() if plan["closure"] is None else set(plan["closure"]["envelope"]["last_observation_ids"])
    later = any(row["envelope"]["observation_id"] not in closed_ids for row in plan["observations"])
    coverage_complete = plan["closure"] is not None and not missing_slots and not incomplete and not conflicts and not later
    usage = {key: None if unknown else value for key, value in spent_totals.items()}
    result = {"scope_id": plan["scope_id"], "revision": plan["revision"], "spent": spent_totals,
        "outstanding": occupied_totals, "committed": committed, "usage": usage, "usage_unknown": unknown,
        "expected_attempt_ids": [row["attempt_id"] for row in plan["attempts"]], "observed_attempt_ids": observed,
        "missing_attempt_ids": [row["attempt_id"] for row in plan["attempts"] if row["attempt_id"] not in observed],
        "incomplete_attempt_ids": incomplete, "missing_slots": missing_slots, "conflicting_attempt_ids": conflicts,
        "missing_sequence_ranges": missing_sequences,
        "coverage_closed": plan["closure"] is not None, "coverage_complete": coverage_complete,
        "cost_unit": baseline["cost_unit"],
        "cost_subtotals": [{"cost_unit": row["cost_unit"], "known_subtotal": _sum(row["values"])} for row in cost_groups.values()]}
    if limits is not None:
        limits = validate_document("usage", limits)
        if any(value is None for value in limits.values()):
            refuse("LIMIT_UNKNOWN")
        result["remaining"] = {key: _difference(str(limits[key]), str(committed[key])) for key in USAGE_FIELDS}
        for key in COUNT_FIELDS:
            result["remaining"][key] = int(result["remaining"][key])
        result["overrun"] = [key for key in USAGE_FIELDS if _compare(str(committed[key]), str(limits[key])) > 0]
    return _clone(result)


def report(plan):
    """Return a versioned scoped report; missing observation never implies zero."""
    plan = validate_document(PLAN, plan)
    value = projection(plan)
    human = None
    observers = sorted((row["envelope"]["human_observer"] for row in plan["observations"]
        if row["envelope"]["human_observer"] is not None and row["envelope"]["provenance"] == "measured"),
        key=lambda observer: _time(observer["observed_through_at"]))
    if value["coverage_complete"] and observers:
        start = _time(plan["opened_at"])
        end = _time(plan["closure"]["envelope"]["closed_at"])
        baseline = plan["observations"][0]["envelope"]["human_observer"]
        if (baseline is not None and baseline["human_interventions"] is not None
                and _time(baseline["started_at"]) <= start <= _time(baseline["observed_through_at"])):
            eligible = [observer for observer in observers if observer["observer_id"] == baseline["observer_id"]
                and _time(observer["started_at"]) <= start and _time(observer["observed_through_at"]) >= end
                and observer["human_interventions"] is not None and not observer["gaps"] and not observer["exclusions"]]
            continuous = all(observer["observer_id"] == baseline["observer_id"]
                and observer["started_at"] == baseline["started_at"] and observer["human_interventions"] is not None
                and not observer["gaps"] and not observer["exclusions"] for observer in observers)
            if continuous:
                continuous = all(_time(right["observed_through_at"]) >= _time(left["observed_through_at"])
                    and right["human_interventions"] >= left["human_interventions"]
                    for left, right in zip(observers, observers[1:], strict=False))
            if eligible and continuous:
                human = max(observer["human_interventions"] for observer in eligible)
    return validate_document(REPORT, {"schema_version": REPORT, "binding": plan["binding"], "scope_id": plan["scope_id"],
        "plan_sha256": digest(plan), "coverage": value,
        "totals": value["usage"] if value["coverage_complete"] else {key: None for key in USAGE_FIELDS},
        "observed_human_interventions": human,
        "limitations": [] if value["coverage_complete"] else ["Declared scope is not completely observed."],
        "authorized": False, "evidence_accepted": False})
