"""Transport-neutral, bounded agent envelopes; validation grants no authority.

Controller, grant, receipt and qualification references are inert declarations.
The host must authenticate them and canonical owners must accept evidence. The
document APIs check serialization and fixed semantics; contextual job/result
validation additionally requires an explicit clock and stored job basis.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any, NoReturn, TypedDict

from .errors import UsageError
from .onboarding_contract import _matches

JOB = "research-host-job/v1"
JOB_V2 = "research-host-job/v2"
JOB_V3 = "research-host-job/v3"
JOB_V4 = "research-host-job/v4"
EXECUTION_OBSERVATION = "research-execution-observation/v1"
RESULT = "research-host-result/v1"
DECISION = "research-decision/v1"
MAX_BYTES = 1_048_576
MAX_DEPTH = 16
MAX_NODES = 16_384
MAX_LEASE_SECONDS = 600
ROLES = ("host_worker", "evaluator")
OPERATIONS = ("generate", "acquire", "evaluate", "probe", "poll", "cancel", "schedule", "deliver")
RESULT_OUTCOMES = ("pending", "incomplete", "unsupported", "succeeded", "failed", "cancelled", "unavailable")
DECISION_OUTCOMES = (
    "accepted", "partial", "abstained", "rejected", "stop_budget", "blocked_external",
    "policy_incompatible", "revise", "acquire_more", "retry", "pending", "incomplete", "unsupported",
)
_UTC_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|\+00:00)$(?![\s\S])"
)


class WorkflowBudgets(TypedDict):
    """Finite ceilings declared by a host; their presence does not grant use."""

    model_calls: int
    tool_calls: int
    tokens: int
    cost: str


class WorkflowQualifications(TypedDict):
    """References to qualification evidence, requiring separate authentication."""

    assurance: str
    status: str
    evaluator_ref: str | None
    qualification_ref: str | None
    context_basis: str | None


class ObservedUsage(TypedDict):
    """Measured usage; null means unknown and cannot reserve execution budget.

    Elapsed seconds preserve finite fractional JSON numbers without rounding.
    Counts remain integers and cost remains an exact decimal string when known.
    """

    seconds: int | float | None
    model_calls: int | None
    tool_calls: int | None
    tokens: int | None
    cost: str | None


class AgentJob(TypedDict):
    schema_version: str
    id: str
    operation: str
    role: str
    controller_ref: str
    grant_ref: str
    input_id: str
    basis_id: str
    policy_id: str
    policy_version: str
    prompt_version: str
    revision_precondition: str | None
    original_text: str | None
    scope: list[str]
    required_capabilities: list[str]
    payload: dict[str, Any]
    issued_utc: str
    deadline_utc: str
    timeout_seconds: int
    max_attempts: int
    expected_result_schema: str
    budgets: WorkflowBudgets
    qualifications: WorkflowQualifications


class AgentResult(TypedDict):
    schema_version: str
    job_id: str
    job_sha256: str
    operation: str
    role: str
    input_id: str
    basis_id: str
    policy_id: str
    controller_ref: str
    grant_ref: str
    deadline_utc: str
    controller_receipt_ref: str
    outcome: str
    observed_usage: ObservedUsage
    artifact_ref: str | None
    qualifications: WorkflowQualifications


class DecisionRecord(TypedDict):
    schema_version: str
    id: str
    job_id: str
    input_id: str
    basis_id: str
    policy_id: str
    policy_version: str
    prompt_version: str
    role: str
    controller_ref: str
    revision_precondition: str | None
    original_text: str | None
    outcome: str
    reason: str
    rationale: str
    source_refs: list[str]
    accounting: dict[str, int]
    uncertainty: dict[str, Any]
    retry_budget: dict[str, int]
    validation_provenance: list[dict[str, Any]]
    qualifications: WorkflowQualifications
    authorized: bool
    evidence_accepted: bool


class WorkflowContractError(UsageError):
    """A workflow document, bound, clock or stored correlation was refused."""


def _refuse(reason: str, field: str = "/") -> NoReturn:
    raise WorkflowContractError(
        f"WORKFLOW_{reason}", "Workflow contract validation refused the input.",
        recoverable=False,
        remediation="Use the published closed schema and the original stored job basis; authenticate references separately.",
        details={"field": field},
    ) from None


def _string(maximum: int = 256) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"^[^\x00-\x1f\x7f]*\S[^\x00-\x1f\x7f]*$(?![\s\S])"}


def _object(**fields: Any) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _array(item: dict[str, Any], maximum: int = 64, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


def _integer(maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _nullable(shape: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [shape, {"type": "null"}]}


def schemas() -> dict[str, dict[str, Any]]:
    """Return detached JSON Schema documents for the supported envelope versions.

    Calendar validity, lease arithmetic and stored-parent/result correlation
    also require the documented semantic validators. Schemas confer no grants.
    """
    ident = {**_string(128), "pattern": r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$(?![\s\S])"}
    original = _nullable({"type": "string", "minLength": 1, "maxLength": 65_536})
    utc = {"type": "string", "maxLength": 32, "pattern": _UTC_PATTERN, "format": "date-time"}
    money = {"type": "string", "maxLength": 16, "pattern": r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$(?![\s\S])"}
    budgets = _object(model_calls=_integer(50), tool_calls=_integer(100), tokens=_integer(100_000), cost=money)
    qualifications = _object(
        assurance={"enum": ["unqualified", "artifact_checked", "host_enforced"]},
        status={"enum": ["declared", "unavailable", "unknown"]},
        evaluator_ref=_nullable(_string()), qualification_ref=_nullable(_string()), context_basis=_nullable(_string()),
    )
    payloads = {
        "generate": _object(question={"type": "string", "minLength": 1, "maxLength": 65_536}),
        "acquire": _object(request_id=ident, source_ref=_string(4096)),
        "evaluate": _object(claim_id=ident, packet_id=_string()),
        "probe": _object(capability=_string(128)),
        "poll": _object(job_id=ident), "cancel": _object(job_id=ident),
        "schedule": _object(work_id=ident),
        "deliver": _object(artifact_id=_string(), destination_ref=_string()),
    }
    common = dict(
        schema_version={"const": JOB}, id=ident, role={"enum": list(ROLES)},
        controller_ref=_string(), grant_ref=_string(), input_id=_string(), basis_id=_string(), policy_id=_string(),
        policy_version=_string(128), prompt_version=_string(), revision_precondition=_nullable(_string()),
        original_text=original, scope={**_array(_string(4096), minimum=1), "uniqueItems": True},
        required_capabilities={**_array(_string(128), 16), "uniqueItems": True},
        issued_utc=utc, deadline_utc=utc, timeout_seconds=_integer(MAX_LEASE_SECONDS, 1),
        max_attempts=_integer(3, 1), expected_result_schema={"const": RESULT}, budgets=budgets,
        qualifications=qualifications,
    )
    jobs = []
    for operation, payload in payloads.items():
        fields = {**common, "operation": {"const": operation}, "payload": payload}
        if operation not in {"poll", "cancel"}:
            fields["role"] = {"const": "evaluator" if operation == "evaluate" else "host_worker"}
        if operation in {"poll", "cancel", "probe"}:
            fields["original_text"] = {"type": "null"}
        jobs.append(_object(**fields))
    job = {"anyOf": jobs}
    result = _object(
        schema_version={"const": RESULT}, job_id=ident,
        job_sha256={"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"},
        operation={"enum": list(OPERATIONS)}, role={"enum": list(ROLES)},
        input_id=_string(), basis_id=_string(), policy_id=_string(), controller_ref=_string(), grant_ref=_string(),
        deadline_utc=utc, controller_receipt_ref=_string(), outcome={"enum": list(RESULT_OUTCOMES)},
        observed_usage=_object(
            seconds=_nullable({"type": "number", "minimum": 0, "maximum": MAX_LEASE_SECONDS}),
            model_calls=_nullable(_integer(50)), tool_calls=_nullable(_integer(100)),
            tokens=_nullable(_integer(100_000)), cost=_nullable(money),
        ),
        artifact_ref=_nullable(_string(4096)), qualifications=qualifications,
    )
    decision = _object(
        schema_version={"const": DECISION}, id=ident, job_id=ident, input_id=_string(), basis_id=_string(),
        policy_id=_string(), policy_version=_string(128), prompt_version=_string(), role={"enum": list(ROLES)},
        controller_ref=_string(), revision_precondition=_nullable(_string()), original_text=original,
        outcome={"enum": list(DECISION_OUTCOMES)}, reason=_string(128),
        rationale={"type": "string", "minLength": 1, "maxLength": 4096},
        source_refs={**_array(_string(4096)), "uniqueItems": True},
        accounting=_object(original_count=_integer(1_000_000), proposed_count=_integer(1_000_000),
                           unresolved_count=_integer(1_000_000)),
        uncertainty=_object(status={"enum": ["not_assessed", "unknown", "bounded"]},
                            description={"type": "string", "maxLength": 4096},
                            confidence=_nullable({"type": "string", "pattern": r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$(?![\s\S])"}),
                            interpretation={"const": "agent_self_assessment_not_probability"}),
        retry_budget=_object(max_attempts=_integer(3, 1), attempts_used=_integer(3), attempts_remaining=_integer(3)),
        validation_provenance=_array(_object(
            owner=_string(128), check=_string(128), reference=_string(4096), basis_id=_string(),
            verdict={"enum": ["pass", "fail", "unknown"]},
        )),
        qualifications=qualifications, authorized={"const": False}, evidence_accepted={"const": False},
    )
    job_v2 = copy.deepcopy(job)
    for branch in job_v2["anyOf"]:
        branch["properties"]["schema_version"] = {"const": JOB_V2}
        branch["properties"]["expected_result_schema"] = {"const": EXECUTION_OBSERVATION}
    job_v3 = copy.deepcopy(job_v2)
    for branch in job_v3["anyOf"]:
        branch["properties"]["schema_version"] = {"const": JOB_V3}
        branch["properties"].update(task_role={"enum": ["coverage", "planner", "researcher", "evaluator",
            "critic", "adjudicator", "repair", "integration", "release"]},
            role_scope_sha256={"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"},
            context_id=_string(), packet_sha256={"type": "string", "pattern": r"^[a-f0-9]{64}$(?![\s\S])"})
        branch["required"].extend(["task_role", "role_scope_sha256", "context_id", "packet_sha256"])
    job_v4 = copy.deepcopy(job_v3)
    for branch in job_v4["anyOf"]:
        branch["properties"]["schema_version"] = {"const": JOB_V4}
        branch["properties"]["task_role"]["enum"].append("calculation")
    return copy.deepcopy({name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
                          for name, shape in ((JOB, job), (JOB_V2, job_v2), (JOB_V3, job_v3), (JOB_V4, job_v4), (RESULT, result), (DECISION, decision))})


def schema_document(name: str) -> dict[str, Any]:
    """Return one supported closed schema; unknown versions fail explicitly."""
    catalog = schemas()
    if type(name) is not str or name not in catalog:
        _refuse("VERSION_UNSUPPORTED", "/schema_version")
    return catalog[name]


def schema_catalog() -> dict[str, Any]:
    """Advertise only inert document support, limits and semantic requirements."""
    return {
        "schema_ids": sorted(schemas()), "roles": list(ROLES), "operations": list(OPERATIONS),
        "limits": {"bytes": MAX_BYTES, "depth": MAX_DEPTH, "nodes": MAX_NODES, "lease_seconds": MAX_LEASE_SECONDS},
        "authority_granted": False, "evidence_accepted": False,
        "context_requirements": {"job": "explicit UTC clock; stored parent for poll/cancel", "result": "complete stored job and explicit UTC clock"},
    }


def _bounded_tree(value: Any) -> None:
    # Exit markers track ancestors, allowing harmless shared values but refusing cycles.
    pending = [(value, 1, False)]
    ancestors: set[int] = set()
    nodes = 0
    string_bytes = 0
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
            children = [child for pair in item.items() for child in pair] if type(item) is dict else item
            if type(item) is dict and any(type(key) is not str for key in item):
                _refuse("INVALID")
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


def _bounded_nesting(text: str) -> None:
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


def _canonical(value: Any) -> bytes:
    chunks = []
    size = 0
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for chunk in encoder.iterencode(value):
            raw = chunk.encode("utf-8")
            size += len(raw)
            if size > MAX_BYTES:
                _refuse("LIMIT")
            chunks.append(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _refuse("INVALID")
    return b"".join(chunks)


def _utc(value: str, field: str) -> datetime:
    if type(value) is not str or len(value) > 32 or re.fullmatch(_UTC_PATTERN, value) is None:
        _refuse("INVALID", field)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _refuse("INVALID", field)


def _semantic(name: str, value: dict[str, Any]) -> None:
    if name in {JOB, JOB_V2, JOB_V3, JOB_V4}:
        issued = _utc(value["issued_utc"], "/issued_utc")
        deadline = _utc(value["deadline_utc"], "/deadline_utc")
        lease = (deadline - issued).total_seconds()
        if not 0 < lease <= MAX_LEASE_SECONDS or value["timeout_seconds"] > lease:
            _refuse("INVALID", "/deadline_utc")
        if value["operation"] == "generate" and value["original_text"] != value["payload"]["question"]:
            _refuse("INVALID", "/original_text")
        for field in ("scope", "required_capabilities"):
            if len(value[field]) != len(set(value[field])):
                _refuse("INVALID", f"/{field}")
    elif name == RESULT:
        _utc(value["deadline_utc"], "/deadline_utc")
        seconds = value["observed_usage"]["seconds"]
        if seconds is not None and (type(seconds) not in (int, float) or not 0 <= seconds <= MAX_LEASE_SECONDS):
            _refuse("INVALID", "/observed_usage/seconds")
        expected_role = "evaluator" if value["operation"] == "evaluate" else "host_worker"
        if value["operation"] not in {"poll", "cancel"} and value["role"] != expected_role:
            _refuse("INVALID", "/role")
        if value["outcome"] == "succeeded" and value["artifact_ref"] is None:
            _refuse("INVALID", "/artifact_ref")
        if value["outcome"] in {"pending", "unsupported", "unavailable"} and value["artifact_ref"] is not None:
            _refuse("INVALID", "/artifact_ref")
    else:
        accounting = value["accounting"]
        if accounting["proposed_count"] + accounting["unresolved_count"] != accounting["original_count"]:
            _refuse("INVALID", "/accounting")
        retries = value["retry_budget"]
        if retries["attempts_used"] + retries["attempts_remaining"] != retries["max_attempts"]:
            _refuse("INVALID", "/retry_budget")
        if len(value["source_refs"]) != len(set(value["source_refs"])):
            _refuse("INVALID", "/source_refs")


def validate_document(name: str, value: Any) -> dict[str, Any]:
    """Validate fixed document semantics and return a detached plain dictionary.

    This does not check current expiry, parent correlation or authentication.
    Use validate_job/validate_result before dispatching or consuming a result.
    """
    schema = schema_document(name)
    _bounded_tree(value)
    if type(value) is dict and "schema_version" in value and value["schema_version"] != name:
        _refuse("VERSION_UNSUPPORTED", "/schema_version")
    if name == RESULT:
        # The existing shape matcher has integer primitives. The finite number
        # type and bounds for fractional elapsed seconds are checked above and
        # in _semantic, while all other closed fields use the shared matcher.
        schema["properties"]["observed_usage"]["properties"]["seconds"]["anyOf"][0].pop("type")
    try:
        _matches(value, schema)
    except UsageError as exc:
        reason = "LIMIT" if exc.error_code == "ONBOARDING_LIMIT" else "INVALID"
        _refuse(reason, exc.details.get("field", "/"))
    _semantic(name, value)
    return json.loads(_canonical(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("INVALID")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    _refuse("INVALID")


def decode_document(name: str, raw: bytes) -> dict[str, Any]:
    """Decode bounded UTF-8 JSON, refusing duplicate keys and nonfinite numbers."""
    schema_document(name)
    if type(raw) is not bytes:
        _refuse("INVALID")
    if not 0 < len(raw) <= MAX_BYTES:
        _refuse("LIMIT")
    try:
        text = raw.decode("utf-8")
        _bounded_nesting(text)
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError):
        _refuse("INVALID")
    return validate_document(name, value)


def encode_document(name: str, value: dict[str, Any]) -> bytes:
    """Return bounded canonical JSON, preserving exact text and qualifications."""
    return _canonical(validate_document(name, value))


def document_sha256(name: str, value: dict[str, Any]) -> str:
    """Bind every field of a valid document; a digest is not authentication."""
    return hashlib.sha256(encode_document(name, value)).hexdigest()


def validate_job(value: Any, *, now_utc: str, parent_job: Any = None) -> dict[str, Any]:
    """Check a live lease and, for poll/cancel, the original stored parent basis.

    The explicit clock must be trusted by the host. A per-call timeout is capped
    by remaining lease time by the executor; this validator cannot authenticate
    the clock, references or the caller's stored parent.
    """
    name = value.get("schema_version") if type(value) is dict else JOB
    if name not in {JOB, JOB_V2, JOB_V3, JOB_V4}:
        _refuse("VERSION_UNSUPPORTED", "/schema_version")
    job = validate_document(name, value)
    now = _utc(now_utc, "/now_utc")
    if now < _utc(job["issued_utc"], "/issued_utc"):
        _refuse("INVALID", "/issued_utc")
    if now >= _utc(job["deadline_utc"], "/deadline_utc"):
        _refuse("DEADLINE_EXPIRED", "/deadline_utc")
    if job["operation"] in {"poll", "cancel"}:
        if parent_job is None:
            _refuse("CONTEXT_REQUIRED", "/payload/job_id")
        parent = validate_document(name, parent_job)
        if parent["operation"] in {"poll", "cancel"}:
            _refuse("CORRELATION_MISMATCH", "/payload/job_id")
        if job["payload"]["job_id"] != parent["id"] or job["id"] == parent["id"]:
            _refuse("CORRELATION_MISMATCH", "/payload/job_id")
        pinned = (
            "input_id", "basis_id", "policy_id", "policy_version", "prompt_version", "revision_precondition",
            "controller_ref", "grant_ref", "role", "scope", "required_capabilities", "issued_utc", "deadline_utc",
            "expected_result_schema", "budgets", "max_attempts", "qualifications",
        )
        for field in pinned:
            if job[field] != parent[field]:
                _refuse("CORRELATION_MISMATCH", f"/{field}")
        if job["timeout_seconds"] > parent["timeout_seconds"]:
            _refuse("CORRELATION_MISMATCH", "/timeout_seconds")
    elif parent_job is not None:
        _refuse("INVALID", "/payload")
    return job


def validate_result(value: Any, *, job: Any, now_utc: str, parent_job: Any = None) -> dict[str, Any]:
    """Correlate a result with its full stored live job without accepting evidence.

    Observed usage is a declaration whose telemetry provenance belongs to the
    host. Unknown metrics remain null; known usage above the job budget is
    retained within public representation bounds for the policy gate, never
    clipped. Budget reservation and enforcement belong to the executor/policy.
    Qualifications must reproduce the stored job metadata. New qualification
    evidence belongs in a referenced artifact and its owner's intake contract.
    """
    result = validate_document(RESULT, value)
    basis = validate_job(job, now_utc=now_utc, parent_job=parent_job)
    if result["job_id"] != basis["id"] or result["job_sha256"] != document_sha256(JOB, basis):
        _refuse("CORRELATION_MISMATCH", "/job_sha256")
    for field in (
        "operation", "role", "input_id", "basis_id", "policy_id", "controller_ref", "grant_ref", "deadline_utc",
        "qualifications",
    ):
        if result[field] != basis[field]:
            _refuse("CORRELATION_MISMATCH", f"/{field}")
    return result


def validate_decision(value: Any) -> dict[str, Any]:
    """Validate an inert proposal; an accepted outcome label cannot accept evidence."""
    return validate_document(DECISION, value)
