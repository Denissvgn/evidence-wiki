"""Bounded cited semantic findings; native owners retain all review authority."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_execution import ExecutionError, _ref
from .mutation_contracts import MutationError, _shape, canonical, detached

PLAN = "research-semantic-judging-plan/v1"
INPUT = "research-semantic-judging-input/v1"
FINDINGS = "research-semantic-findings/v1"
ADJUDICATION = "research-semantic-adjudication/v1"
DIMENSIONS = ("support", "scoped_correctness", "missing_material_evidence",
              "appropriate_abstention", "unnecessary_refusal")
CRITERIA = ("support", "source_suitability", "scope", "time", "units", "counterevidence")
MAX_INPUT_BYTES = 262_144
MAX_OUTPUT_BYTES = 262_144
MAX_UNITS = 100
_FIRST_PASS_ROLES = ("evidence", "scope_units", "counterevidence")
_PROTOCOL_SOURCES = ("semantic_contracts.py", "semantic_judging.py", "semantic_reporting.py")
_SOURCE_ID = r"^source-[1-9][0-9]?$(?![\s\S])"
_UNIT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"
_HASH = r"^sha256:[a-f0-9]{64}$(?![\s\S])"


def _fail(reason: str) -> None:
    raise ExecutionError("semantic_" + reason) from None


def _protocol_hashes() -> tuple[tuple[str, str], ...]:
    try:
        return tuple((name, hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest())
                     for name in _PROTOCOL_SOURCES)
    except OSError:
        _fail("protocol_unavailable")


@dataclass(frozen=True)
class SemanticJudgingPlan:
    """Explicit three-pass selection and independently calibrated investigation."""

    evidence_config_ref: str
    scope_units_config_ref: str
    counterevidence_config_ref: str
    max_input_bytes: int = MAX_INPUT_BYTES
    _source_hashes: tuple[tuple[str, str], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        configs = (self.evidence_config_ref, self.scope_units_config_ref, self.counterevidence_config_ref)
        for config in configs:
            _ref(config)
        if len(set(configs)) != 3:
            _fail("configurations_duplicate")
        if type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= MAX_INPUT_BYTES:
            _fail("input_bound_invalid")
        object.__setattr__(self, "_source_hashes", _protocol_hashes())

    def document(self) -> dict[str, Any]:
        return {"schema_version": PLAN, "evidence_config_ref": self.evidence_config_ref,
                "scope_units_config_ref": self.scope_units_config_ref,
                "counterevidence_config_ref": self.counterevidence_config_ref,
                "max_input_bytes": self.max_input_bytes, "dimensions": list(DIMENSIONS),
                "protocol_source_sha256": dict(self._source_hashes)}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.document())).hexdigest()

    def role_for(self, config_ref: str) -> str:
        _ref(config_ref)
        for role, selected in zip(_FIRST_PASS_ROLES, (self.evidence_config_ref,
                self.scope_units_config_ref, self.counterevidence_config_ref), strict=True):
            if selected == config_ref:
                return role
        _fail("configuration_unavailable")

    def check_profile(self, profile: Any) -> None:
        """Check configured roles and separation, without claiming live qualification."""
        if self._source_hashes != _protocol_hashes():
            _fail("protocol_changed")
        if getattr(profile, "review_services", None) is not None:
            _fail("review_services_unsupported")
        scopes = getattr(profile, "role_scopes", None)
        calibration = getattr(profile, "calibration", None)
        disagreement = getattr(profile, "disagreement", None)
        if scopes is None or calibration is None or disagreement is None:
            _fail("scoped_calibration_required")
        adjudicator = disagreement.adjudicator_config_ref
        if adjudicator is None:
            _fail("adjudicator_required")
        configs = (self.evidence_config_ref, self.scope_units_config_ref, self.counterevidence_config_ref)
        if adjudicator in configs:
            _fail("adjudicator_is_first_pass")
        selected = []
        for config, task_role in zip(configs, ("evaluator", "evaluator", "critic"), strict=True):
            role, scope = profile.role(config), scopes.scope(config)
            if role.role != "evaluator" or scope.authority_role != "evaluator" or scope.task_role != task_role:
                _fail("role_invalid")
            if calibration.selection(config).config_ref != config:
                _fail("calibration_selection_changed")
            selected.append(role)
        controllers = {role.controller_ref for role in selected}
        generators = {role.controller_ref for role in profile.roles if role.role == "host_worker"}
        if len(controllers) != 3 or controllers.intersection(generators):
            _fail("controller_shared")
        auxiliary, auxiliary_scope = profile.role(adjudicator), scopes.scope(adjudicator)
        if (auxiliary.role != "evaluator" or auxiliary_scope.authority_role != "evaluator"
                or auxiliary_scope.task_role != "adjudicator"):
            _fail("adjudicator_role_invalid")
        if calibration.selection(adjudicator).config_ref != adjudicator:
            _fail("calibration_selection_changed")
        first_passes = {role.config_ref for role in profile.roles
                        if scopes.scope(role.config_ref).task_role in {"evaluator", "critic"}}
        if first_passes != set(configs):
            _fail("first_pass_selection_changed")
        if any(other.config_ref != adjudicator and other.controller_ref == auxiliary.controller_ref
               for other in profile.roles):
            _fail("adjudicator_controller_shared")
        disagreement.check_profile(profile)

    def check_policy(self, policy: Any, profile: Any) -> None:
        """Require the exact native role pairs; human requirements remain native."""
        self.check_profile(profile)
        frozen = policy.to_dict()
        if frozen.get("review_services") is not None:
            _fail("review_services_unsupported")
        rows = frozen.get("required_evaluators")
        if type(rows) is not list or len(rows) != 3:
            _fail("policy_selection_changed")
        selected = {self.evidence_config_ref: "evaluator", self.scope_units_config_ref: "evaluator",
                    self.counterevidence_config_ref: "critic"}
        seen = set()
        for row in rows:
            if (type(row) is not dict or set(row) != {"role", "profile_ref", "controller_ref", "human_required"}
                    or type(row["profile_ref"]) is not str or row["profile_ref"] not in selected
                    or row["profile_ref"] in seen or row["role"] != selected[row["profile_ref"]]
                    or row["controller_ref"] != profile.role(row["profile_ref"]).controller_ref
                    or type(row["human_required"]) is not bool):
                _fail("policy_selection_changed")
            seen.add(row["profile_ref"])


def _obj(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _text(maximum: int = 4096) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _array(items: dict[str, Any], maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def _nullable(shape: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [shape, {"type": "null"}]}


def schemas() -> dict[str, dict[str, Any]]:
    """Return detached closed bounds; pointer and denominator relations are checked."""
    identifier = {**_text(96), "pattern": _UNIT_ID}
    source_id = {**_text(16), "pattern": _SOURCE_ID}
    hashed = {**_text(71), "pattern": _HASH}
    native = _obj(**{criterion: {"type": "string", "enum": ["pass", "fail", "unknown"]}
                    for criterion in CRITERIA})
    anchor = _obj(pointer=_text(1024), start={"type": "integer", "minimum": 0, "maximum": MAX_INPUT_BYTES},
                  end={"type": "integer", "minimum": 1, "maximum": MAX_INPUT_BYTES}, quote=_text(4096))
    row = _obj(unit_id=identifier, verdict={"type": "string", "enum": ["pass", "fail", "unknown", "not_applicable"]},
               reason=_text(1024), severity={"type": "string", "enum": ["none", "minor", "major", "critical", "unknown"]},
               uncertainty={"type": "string", "enum": ["low", "medium", "high", "unknown"]}, anchors=_array(anchor, 4))
    checks = _obj(**{dimension: _array(row, MAX_UNITS if dimension == "missing_material_evidence" else 1, 1)
                    for dimension in DIMENSIONS})
    objection = _obj(criterion={"type": "string", "enum": list(CRITERIA)},
                     source_refs=_array(source_id, 16, 1), rationale=_text(), material={"type": "boolean"})
    findings = _obj(schema_version={"const": FINDINGS}, verdicts=native, rationale=_text(),
                    objections=_array(objection, 16), checks=checks)
    adjudication = _obj(schema_version={"const": ADJUDICATION}, rationale=_text(), checks=checks)
    metadata = {"type": "object", "properties": {name: _nullable(_text(4096)) for name in
                ("title", "published_at", "source_type", "url")}, "additionalProperties": False}
    metadata["properties"]["authors"] = _nullable(_array(_text(256), 16))
    source = _obj(source_id=source_id, record_sha256=hashed, metadata=metadata, content=_text(65536))
    evidence = _obj(source_id=source_id, record_sha256=hashed, observed_at=_text(),
                    capture={"type": "string", "enum": ["primary", "excerpt", "summary", "unknown"]},
                    quote=_nullable(_text(65536)), location_hint=_nullable(_text(65536)),
                    anchor=_nullable(_obj(pointer=_text(512), expected=_text(512))))
    candidate = _obj(text=_text(65536), scope=_text(65536), time=_text(65536), units=_text(65536),
                     evidence=_array(evidence, 16))
    owner_unit = _obj(unit_id=identifier, applicable={"type": "boolean"})
    units = _obj(**{dimension: _array(owner_unit, MAX_UNITS if dimension == "missing_material_evidence" else 1, 1)
                   for dimension in DIMENSIONS})
    semantic = _obj(schema_version={"const": INPUT}, role={"type": "string", "enum": [*_FIRST_PASS_ROLES, "adjudicator"]},
                    original_text=_text(65536), required_units=_array(_obj(unit_id=identifier, text=_text(65536)), MAX_UNITS, 1),
                    sources=_array(source, 64), candidate_units=_array(candidate, MAX_UNITS),
                    candidate_behavior={"type": "string", "enum": ["asserted", "abstained", "refused"]},
                    basis_id=hashed, units=units)
    source_hashes = _obj(**{name: {**_text(64), "pattern": r"^[a-f0-9]{64}$(?![\s\S])"}
                           for name in _PROTOCOL_SOURCES})
    plan = _obj(schema_version={"const": PLAN}, evidence_config_ref=_text(256), scope_units_config_ref=_text(256),
                counterevidence_config_ref=_text(256), max_input_bytes={"type": "integer", "minimum": 1, "maximum": MAX_INPUT_BYTES},
                dimensions={"const": list(DIMENSIONS)}, protocol_source_sha256=source_hashes)
    return detached({name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **shape}
                     for name, shape in ((PLAN, plan), (INPUT, semantic), (FINDINGS, findings), (ADJUDICATION, adjudication))})


def schema_document(name: str) -> dict[str, Any]:
    if type(name) is not str or name not in {PLAN, INPUT, FINDINGS, ADJUDICATION}:
        _fail("version_unsupported")
    return schemas()[name]


def _bounded(value: Any, maximum: int, reason: str) -> Any:
    try:
        result = detached(value)
        if len(canonical(result)) > maximum:
            _fail(reason + "_limit")
        return result
    except MutationError as error:
        _fail(reason + ("_limit" if error.error_code == "MUTATION_LIMIT" else "_invalid"))


def _match(value: Any, shape: dict[str, Any], reason: str) -> None:
    try:
        _shape(value, shape)
    except MutationError:
        _fail(reason)


def _validate_units(units: Any, required_ids: list[str] | None = None) -> None:
    _match(units, schema_document(INPUT)["properties"]["units"], "units_invalid")
    expected = {"support": {"claim-1"}, "scoped_correctness": {"claim-1"},
                "appropriate_abstention": {"original-1"}, "unnecessary_refusal": {"original-1"}}
    if required_ids is not None:
        expected["missing_material_evidence"] = set(required_ids)
    for dimension in DIMENSIONS:
        ids = [row["unit_id"] for row in units[dimension]]
        if len(set(ids)) != len(ids) or dimension in expected and set(ids) != expected[dimension]:
            _fail("units_scope_invalid")


def checked_input(worker_input: Any, *, max_input_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    """Check exact owner scope and finite input; no source correctness is inferred."""
    if type(max_input_bytes) is not int or not 1 <= max_input_bytes <= MAX_INPUT_BYTES:
        _fail("input_bound_invalid")
    value = _bounded(worker_input, max_input_bytes, "input")
    if (type(value) is not dict or not {"packet", "semantic"} <= set(value)
            or set(value) - {"packet", "semantic", "first_passes", "purpose", "instructions", "output_contract"}
            or type(value["packet"]) is not dict):
        _fail("input_invalid")
    semantic = value["semantic"]
    _match(semantic, schema_document(INPUT), "input_invalid")
    required_ids = [row["unit_id"] for row in semantic["required_units"]]
    if (len(set(required_ids)) != len(required_ids)
            or any(re.fullmatch(r"unit-[1-9][0-9]{0,2}", identifier) is None for identifier in required_ids)):
        _fail("required_units_invalid")
    _validate_units(semantic["units"], required_ids)
    sources = semantic["sources"]
    allowed = {source["source_id"] for source in sources}
    if len(allowed) != len(sources):
        _fail("sources_duplicate")
    for claim in semantic["candidate_units"]:
        if any(row["source_id"] not in allowed for row in claim["evidence"]):
            _fail("candidate_source_scope_invalid")
    passes = value.get("first_passes")
    if semantic["role"] == "adjudicator":
        if (type(passes) is not list or len(passes) != 3
                or any(type(row) is not dict or set(row) != {"role", "findings"}
                       or type(row["role"]) is not str or row["role"] not in _FIRST_PASS_ROLES for row in passes)
                or {row["role"] for row in passes} != set(_FIRST_PASS_ROLES)):
            _fail("first_passes_invalid")
        for row in passes:
            _validated_output(row["findings"], value, adjudication=False)
    elif "first_passes" in value:
        _fail("first_passes_unexpected")
    return value


def _pointer_string(worker_input: dict[str, Any], pointer: str) -> str:
    if not pointer.startswith("/"):
        _fail("anchor_pointer_invalid")
    parts = pointer[1:].split("/")
    if any(re.search(r"~(?![01])", part) is not None for part in parts):
        _fail("anchor_pointer_invalid")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in parts]
    permitted = (parts == ["semantic", "original_text"]
        or len(parts) == 4 and parts[:2] == ["semantic", "required_units"] and parts[3] == "text"
        or len(parts) == 4 and parts[:2] == ["semantic", "candidate_units"] and parts[3] in {"text", "scope", "time", "units"}
        or len(parts) == 4 and parts[:2] == ["semantic", "sources"] and parts[3] == "content"
        or len(parts) == 3 and parts[:2] == ["packet", "claim"] and parts[2] in {"text", "scope", "time", "units"}
        or parts == ["packet", "question", "question"])
    if not permitted:
        _fail("anchor_scope_invalid")
    current: Any = worker_input
    for part in parts:
        if type(current) is dict:
            if part not in current:
                _fail("anchor_pointer_invalid")
            current = current[part]
        elif type(current) is list:
            if re.fullmatch(r"0|[1-9][0-9]*", part) is None or len(part) > 4 or int(part) >= len(current):
                _fail("anchor_pointer_invalid")
            current = current[int(part)]
        else:
            _fail("anchor_pointer_invalid")
    if type(current) is not str:
        _fail("anchor_target_invalid")
    return current


def _validate_checks(checks: Any, units: dict[str, Any], worker_input: dict[str, Any] | None = None) -> None:
    _match(checks, schema_document(FINDINGS)["properties"]["checks"], "checks_invalid")
    for dimension in DIMENSIONS:
        expected = {row["unit_id"]: row["applicable"] for row in units[dimension]}
        rows = checks[dimension]
        if len(rows) != len(expected) or {row["unit_id"] for row in rows} != set(expected):
            _fail("checks_scope_invalid")
        for row in rows:
            verdict = row["verdict"]
            if (expected[row["unit_id"]] and verdict == "not_applicable"
                    or not expected[row["unit_id"]] and verdict != "not_applicable"):
                _fail("applicability_changed")
            if (verdict == "unknown" and (row["severity"] != "unknown" or row["uncertainty"] != "unknown")
                    or verdict in {"pass", "not_applicable"} and row["severity"] != "none"):
                _fail("check_state_invalid")
            if verdict in {"pass", "fail"} and not row["anchors"]:
                _fail("anchor_required")
            if worker_input is not None:
                for anchor in row["anchors"]:
                    target = _pointer_string(worker_input, anchor["pointer"])
                    if (not 0 <= anchor["start"] < anchor["end"] <= len(target)
                            or target[anchor["start"]:anchor["end"]] != anchor["quote"]):
                        _fail("anchor_quote_invalid")


def _aggregate(rows: list[dict[str, Any]]) -> str | None:
    verdicts = {row["verdict"] for row in rows} - {"not_applicable"}
    return "fail" if "fail" in verdicts else "unknown" if "unknown" in verdicts else "pass" if verdicts else None


def _validated_output(output: Any, worker_input: dict[str, Any], *, adjudication: bool) -> dict[str, Any]:
    value = _bounded(output, MAX_OUTPUT_BYTES, "findings")
    _match(value, schema_document(ADJUDICATION if adjudication else FINDINGS), "findings_invalid")
    _validate_checks(value["checks"], worker_input["semantic"]["units"], worker_input)
    if not adjudication:
        allowed = {source["source_id"] for source in worker_input["semantic"]["sources"]}
        for objection in value["objections"]:
            refs = objection["source_refs"]
            if len(set(refs)) != len(refs) or not set(refs) <= allowed:
                _fail("objection_source_scope_invalid")
        support = _aggregate(value["checks"]["support"])
        native = value["verdicts"]
        if support is not None and support != native["support"]:
            _fail("native_support_conflict")
        scoped = _aggregate(value["checks"]["scoped_correctness"])
        conjunction = ("fail" if any(native[key] == "fail" for key in CRITERIA[:5]) else
                       "unknown" if any(native[key] == "unknown" for key in CRITERIA[:5]) else "pass")
        if scoped is not None and scoped != conjunction:
            _fail("native_scope_conflict")
        missing = _aggregate(value["checks"]["missing_material_evidence"])
        if missing in {"fail", "unknown"} and native["source_suitability"] == native["counterevidence"] == "pass":
            _fail("native_missing_evidence_conflict")
    return value


def metrics(checks: Any, units: Any) -> dict[str, dict[str, Any]]:
    """Compute scoped counts and unresolved ranges, without claiming accuracy."""
    checks, units = _bounded(checks, MAX_OUTPUT_BYTES, "checks"), _bounded(units, MAX_INPUT_BYTES, "units")
    _validate_units(units)
    _validate_checks(checks, units)
    result = {}
    for dimension in DIMENSIONS:
        rows = checks[dimension]
        counts = {verdict: sum(row["verdict"] == verdict for row in rows)
                  for verdict in ("pass", "fail", "unknown", "not_applicable")}
        applicable = sum(row["applicable"] for row in units[dimension])
        passed, unknown = counts["pass"], counts["unknown"]
        result[dimension] = {"expected": len(units[dimension]), "applicable": applicable,
            "passed": passed, "failed": counts["fail"], "unknown": unknown,
            "not_applicable": counts["not_applicable"], "numerator": passed, "denominator": applicable,
            "score": passed / applicable if applicable and not unknown else None,
            "uncertainty_bounds": {"lower": passed / applicable if applicable else None,
                                   "upper": (passed + unknown) / applicable if applicable else None}}
    return result


def checked_findings(output: Any, worker_input: Any, *, adjudication: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode exact cited proposals and compute denominators from the owner input."""
    if type(adjudication) is not bool:
        _fail("adjudication_flag_invalid")
    worker_input = checked_input(worker_input)
    if (worker_input["semantic"]["role"] == "adjudicator") != adjudication:
        _fail("findings_role_invalid")
    value = _validated_output(output, worker_input, adjudication=adjudication)
    if adjudication:
        for prior in worker_input["first_passes"]:
            first = prior["findings"]["checks"]
            for dimension in DIMENSIONS:
                previous = {row["unit_id"]: row["verdict"] for row in first[dimension]}
                if any(row["verdict"] == "pass" and previous[row["unit_id"]] in {"fail", "unknown"}
                       for row in value["checks"][dimension]):
                    _fail("adjudication_promotion")
    return value, metrics(value["checks"], worker_input["semantic"]["units"])
