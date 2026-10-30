"""Compile ordinary UTF-8 objectives into inert, lossless research requests.

Host selections and frozen policy constrain every default and proposal. This
module neither invokes a model nor reads evidence, credentials or the network.
Compilation is separate from setup readiness, authorization and evidence truth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, NoReturn
from urllib.parse import urlsplit

from .errors import UsageError
from .onboarding_contract import _bounded_tree, _matches, encode_document
from .onboarding_schemas import schema_document as onboarding_schema
from .run_policy import RunPolicy

RESULT = "research-request-compilation/v1"
PROPOSAL = "research-planning-proposal/v1"
MAX_BYTES = 1_048_576
MAX_ORIGINAL_BYTES = 65_536
_ID = {"type": "string", "minLength": 1, "maxLength": 128,
       "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$(?![\s\S])"}
# Setup and local research permissions include persistent workspace state
# writes. Model, source and delivery effects remain separately selected.
_ACTION_EFFECTS = {
    "local_setup": "revise", "local_research": "revise",
    "local_pack_authoring": "revise", "same_pack_refresh": "revise",
    "acquisition": "acquire", "discovery": "acquire", "host_capture": "acquire",
    "export": "deliver", "dependency_install": None,
}


class RequestCompilationError(UsageError):
    """A content-free refusal for malformed compilation inputs."""


def _refuse(reason: str, field: str = "/") -> NoReturn:
    raise RequestCompilationError("REQUEST_COMPILATION_REFUSED", "Request compilation refused the input.",
        recoverable=False, details={"reason": reason, "field": field},
        remediation="Supply bounded host selections and proposals without changing frozen authority or policy.") from None


def _object(**fields: Any) -> dict[str, Any]:
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


def _text(maximum: int = 4096) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _array(item: dict[str, Any], maximum: int = 64, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


def _integer(maximum: int = MAX_ORIGINAL_BYTES) -> dict[str, Any]:
    return {"type": "integer", "minimum": 0, "maximum": maximum}


def proposal_schema() -> dict[str, Any]:
    """Closed proposal shape; its confidence is an unverified model assertion."""
    span = _object(original_id=_ID, start_byte=_integer(), end_byte=_integer())
    original = _object(id=_ID, start_byte=_integer(), end_byte=_integer())
    derived = _object(id=_ID, text=_text(), original_ids=_array(_ID, 100, 1), anchors=_array(span, 100, 1))
    interpretation = _object(id=_ID, original_questions=_array(original, 100, 1),
        derived_questions=_array(derived, 200), source_ids=_array(_ID, 200),
        scope=_array(_object(name=_ID, value=_text())), assumptions=_array(_text()),
        rationale=_text(), confidence={"type": "string", "maxLength": 8,
            "pattern": r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$(?![\s\S])"})
    return _object(schema_version={"const": PROPOSAL}, interpretations=_array(interpretation, 16, 1))


def _detach(value: Any, field: str) -> Any:
    try:
        _bounded_tree(value)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_BYTES:
            _refuse("input_bound", field)
        return json.loads(raw)
    except UsageError:
        _refuse("input_invalid_or_bound", field)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _refuse("input_invalid", field)


def _shape(value: Any, shape: dict[str, Any], field: str) -> None:
    try:
        _matches(value, shape, field)
    except UsageError:
        _refuse("input_shape_invalid", field)


def _original(value: str | bytes) -> tuple[bytes, str]:
    try:
        if type(value) is str:
            raw, text = value.encode("utf-8"), value
        elif type(value) is bytes:
            raw, text = value, value.decode("utf-8")
        else:
            _refuse("objective_invalid", "/objective")
    except UnicodeError:
        _refuse("objective_invalid_utf8", "/objective")
    if len(raw) > MAX_ORIGINAL_BYTES:
        _refuse("objective_bound", "/objective")
    if "\0" in text:
        _refuse("objective_invalid", "/objective")
    return raw, text


def _path(value: str):
    path = PureWindowsPath(value) if PureWindowsPath(value).drive else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in value:
        _refuse("host_path_invalid", "/host_scope")
    return path


def _within(locator: str, allowed: str) -> bool:
    """Conservative lexical containment; live filesystem/network checks are separate."""
    if locator == allowed:
        return True
    if "://" in locator or "://" in allowed:
        try:
            item, parent = urlsplit(locator), urlsplit(allowed)
            origin_matches = (item.hostname, item.port) == (parent.hostname, parent.port)
        except ValueError:
            return False
        if item.scheme not in {"https", "http"} or parent.scheme != item.scheme:
            return False
        if item.username is not None or item.password is not None or parent.username is not None or parent.password is not None:
            return False
        if not origin_matches or parent.query or parent.fragment:
            return False
        if any(part in {".", ".."} or "%" in part or "\\" in part for part in item.path.split("/")):
            return False
        base = parent.path.rstrip("/")
        return item.path == base or item.path.startswith(base + "/")
    try:
        return _path(locator).is_relative_to(_path(allowed))
    except RequestCompilationError:
        return False


def _host_shape() -> dict[str, Any]:
    payload = onboarding_schema("onboarding/research_request/v2")["properties"]["payload"]["properties"]
    shape = _object(target=payload["target"], authority=payload["authority"], scope=payload["scope"],
        evidence_policy={"type": "object"}, sources=payload["sources"], domain=payload["domain"],
        host_tools=payload["host_tools"], question_plans=_array({"type": "object"}, 300),
        source_requirements=_array({"type": "object"}, 200))
    shape["required"] = []
    return shape


def _partial(shape: dict[str, Any]) -> dict[str, Any]:
    return {key: _partial(value) if isinstance(value, dict) else
            [_partial(child) if isinstance(child, dict) else child for child in value] if isinstance(value, list) else value
            for key, value in shape.items() if key != "required"}


def _missing(value: Any, shape: dict[str, Any], field: str = "") -> list[str]:
    if isinstance(value, list):
        return [missing for index, item in enumerate(value)
                for missing in _missing(item, shape.get("items", {}), field + str(index) + ".")]
    if not isinstance(value, dict):
        return []
    result = [field + key for key in shape.get("required", []) if key not in value]
    for key, child in shape.get("properties", {}).items():
        if key in value:
            result.extend(_missing(value[key], child, field + key + "."))
    return result


def _defaults_shape() -> dict[str, Any]:
    payload = onboarding_schema("onboarding/research_request/v2")["properties"]["payload"]["properties"]
    shape = _object(language=_text(64), project_name=_text(128), outputs=payload["outputs"], budgets=payload["budgets"])
    shape["required"] = []
    shape["properties"]["budgets"]["required"] = []
    return shape


def _span(raw: bytes, start: int, end: int) -> str:
    if not 0 <= start < end <= len(raw):
        _refuse("proposal_anchor_invalid", "/proposal")
    try:
        text = raw[start:end].decode("utf-8")
    except UnicodeError:
        _refuse("proposal_anchor_invalid", "/proposal")
    if not text.strip():
        _refuse("proposal_anchor_empty", "/proposal")
    return text


def _split(raw: bytes, text: str, ids: list[str]) -> list[dict[str, Any]] | None:
    if len(ids) == 1:
        return [{"id": ids[0], "start_byte": 0, "end_byte": len(raw)}]
    ends = [len(text[:match.end()].encode("utf-8")) for match in re.finditer(r"[?？؟]+", text)]
    if not ends:
        return None
    if not raw[ends[-1]:].strip():
        ends[-1] = len(raw)
    elif ends[-1] < len(raw):
        ends.append(len(raw))
    if len(ends) != len(ids):
        return None
    starts = [0, *ends[:-1]]
    return [{"id": qid, "start_byte": start, "end_byte": end}
            for qid, start, end in zip(ids, starts, ends, strict=True)]


def _interpretation(value: dict[str, Any], raw: bytes, policy: dict[str, Any], host: dict[str, Any]) -> dict[str, Any]:
    originals = value["original_questions"]
    if [row["id"] for row in originals] != policy["scope"]["question_ids"]:
        _refuse("proposal_original_accounting_changed", "/proposal")
    cursor, spans = 0, {}
    for row in originals:
        if row["start_byte"] != cursor:
            _refuse("proposal_original_bytes_discarded", "/proposal")
        _span(raw, row["start_byte"], row["end_byte"])
        spans[row["id"]] = row
        cursor = row["end_byte"]
    if cursor != len(raw):
        _refuse("proposal_original_bytes_discarded", "/proposal")
    seen = set(spans)
    for row in value["derived_questions"]:
        if row["id"] in seen or len(row["original_ids"]) != len(set(row["original_ids"])) or not set(row["original_ids"]) <= spans.keys():
            _refuse("proposal_derived_accounting_invalid", "/proposal")
        seen.add(row["id"])
        if {anchor["original_id"] for anchor in row["anchors"]} != set(row["original_ids"]):
            _refuse("proposal_derived_anchors_invalid", "/proposal")
        for anchor in row["anchors"]:
            original = spans[anchor["original_id"]]
            _span(raw, anchor["start_byte"], anchor["end_byte"])
            if not original["start_byte"] <= anchor["start_byte"] < anchor["end_byte"] <= original["end_byte"]:
                _refuse("proposal_derived_anchors_invalid", "/proposal")
    if len(seen) > min(100, policy["budgets"]["questions"]):
        _refuse("proposal_question_budget_exceeded", "/proposal")
    source_ids = {row["id"] for row in host.get("sources", [])}
    if len(value["source_ids"]) != len(set(value["source_ids"])) or not set(value["source_ids"]) <= source_ids:
        _refuse("proposal_source_not_declared", "/proposal")
    host_scope = {row["name"]: row["value"] for row in host["scope"]}
    if any(host_scope.get(row["name"]) != row["value"] for row in value["scope"]):
        _refuse("proposal_scope_changed", "/proposal")
    return value


def compile_request(objective: str | bytes, *, host_scope: Any = None, policy: RunPolicy | None = None,
                    defaults: Any = None, proposal: Any = None, request_id: str = "research-request") -> dict[str, Any]:
    """Return a compiled declaration or terminal insufficient_context, without prompts.

    Host scope supplies target, authority, material scope and the full evidence
    policy. Optional sources are existing declared locations, never inferred
    observed evidence. RunPolicy freezes original question IDs and ceilings.
    Proposals may supply lossless original byte partitions and anchored derived
    questions; they cannot edit host controls or the frozen policy.
    """
    raw, text = _original(objective)
    original = {"text": text, "encoding": "utf-8", "bytes_base64": base64.b64encode(raw).decode("ascii"),
                "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    result = {"schema_version": RESULT, "status": "insufficient_context", "terminal": True,
        "reason": None, "missing": [], "original": original, "policy_id": None, "policy": None,
        "request": None, "defaults": [], "assumptions": [], "question_anchors": [],
        "interpretations": [], "confidence_provenance": {"basis": "not_evaluated", "value": None},
        "actions_executed": False, "authority_verified": False, "evidence_accepted": False,
        "semantic_adequacy": "not_evaluated", "model_calls": 0}
    if policy is not None and type(policy) is not RunPolicy:
        _refuse("policy_invalid", "/policy")
    frozen = policy.to_dict() if policy is not None else None
    host = _detach({} if host_scope is None else host_scope, "/host_scope")
    chosen_defaults = _detach({} if defaults is None else defaults, "/defaults")
    host_shape = _host_shape()
    _shape(host, _partial(host_shape), "/host_scope")
    _shape(chosen_defaults, _defaults_shape(), "/defaults")
    _shape(request_id, _ID, "/request_id")
    result["missing"] = [name for name in ("target", "authority", "scope", "evidence_policy") if not host.get(name)]
    result["missing"].extend(_missing(host, host_shape))
    if not text.strip():
        result["missing"].append("objective")
    if frozen is None:
        result["missing"].append("policy")
    else:
        result["policy_id"] = frozen["policy_id"]
        result["policy"] = frozen
        if frozen["objective"] != text:
            _refuse("policy_objective_changed", "/policy")
    if result["missing"]:
        result["missing"] = list(dict.fromkeys(result["missing"]))
        result["reason"] = "mandatory_context_missing"
        return result
    if len(text) > 16_384:
        result["reason"] = "objective_exceeds_request_bound"
        return result
    from .pack_discovery import owner

    try:
        evidence_policy = owner("_strict_contract").policy_document(host["evidence_policy"])
    except (Exception, SystemExit):
        _refuse("evidence_policy_invalid", "/host_scope/evidence_policy")
    if (evidence_policy["assurance"] != frozen["assurance"]
            or evidence_policy["human_review"] != frozen["human_review"]):
        _refuse("evidence_policy_requirements_changed", "/host_scope/evidence_policy")
    authority = host["authority"]
    original_actions = list(authority["allowed_actions"])
    action_decisions = []
    for action in original_actions:
        effect = _ACTION_EFFECTS.get(action)
        reason = ("action_not_representable" if effect is None else
                  "effect_not_permitted" if effect not in frozen["allowed_effects"] else
                  "capability_reference_missing" if frozen["host_capability_refs"].get(effect) is None else
                  "retained_declaration")
        action_decisions.append({"action": action, "effect": effect, "reason": reason,
                                 "retained": reason == "retained_declaration"})
    authority["allowed_actions"] = [row["action"] for row in action_decisions if row["retained"]]
    root = _path(host["target"]["writable_root"])
    relative = PurePosixPath(host["target"]["relative_path"])
    if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or not relative.parts:
        _refuse("target_relative_path_invalid", "/host_scope/target")
    if not any(root.is_relative_to(_path(value)) for value in authority["writable_roots"]):
        _refuse("target_outside_host_authority", "/host_scope/target")
    if (len({row["name"] for row in host["scope"]}) != len(host["scope"])
            or any(not row["value"].strip() for row in host["scope"])):
        _refuse("material_scope_invalid", "/host_scope/scope")
    if any(not any(_within(value, allowed) for allowed in frozen["scope"]["source_scope"])
           for value in authority["source_scope"]):
        _refuse("source_scope_exceeds_policy", "/host_scope/authority")
    sources = host.get("sources", [])
    ids = frozen["scope"]["question_ids"]
    if len({row["id"] for row in sources}) != len(sources):
        _refuse("source_identity_duplicate", "/host_scope/sources")
    for source in sources:
        if not set(source["question_ids"]) <= set(ids):
            _refuse("source_question_accounting_invalid", "/host_scope/sources")
        if not any(_within(source["locator"], allowed) for allowed in authority["source_scope"]):
            _refuse("source_location_not_granted", "/host_scope/sources")
    if len(sources) > frozen["budgets"]["sources"]:
        _refuse("source_budget_exceeded", "/host_scope/sources")
    budget_defaults = {"questions": frozen["budgets"]["questions"], "seconds": frozen["budgets"]["seconds"],
        "source_requests": min(1000, frozen["budgets"]["sources"]), "downloads": 0, "bytes": 0}
    budgets = {**budget_defaults, **chosen_defaults.get("budgets", {})}
    ceilings = {"questions": frozen["budgets"]["questions"], "seconds": frozen["budgets"]["seconds"],
                "source_requests": frozen["budgets"]["sources"], "downloads": frozen["budgets"]["tool_calls"]}
    if any(budgets[key] > limit for key, limit in ceilings.items()):
        _refuse("default_budget_exceeds_policy", "/defaults/budgets")
    if "acquire" not in frozen["allowed_effects"] and (budgets["downloads"] or budgets["bytes"]):
        _refuse("default_acquisition_not_granted", "/defaults/budgets")
    optional = {"language": "und", "project_name": "research", "outputs": ["markdown"]}
    effective = {**optional, **{key: value for key, value in chosen_defaults.items() if key != "budgets"}}
    result["defaults"] = [{"field": key, "value": value, "basis": "host_default" if key in chosen_defaults else "conservative_default"}
                          for key, value in effective.items()]
    result["defaults"] += [{"field": "budgets." + key, "value": value,
        "basis": "host_default" if key in chosen_defaults.get("budgets", {}) else "frozen_policy" if key in ceilings else "conservative_default"}
        for key, value in budgets.items()]
    result["defaults"].append({"field": "authority.allowed_actions", "value": authority["allowed_actions"],
        "basis": "intersection_of_host_declaration_frozen_effects_and_capability_references",
        "requested": original_actions, "decisions": action_decisions})
    if proposal is None:
        originals = _split(raw, text, ids)
        if originals is None:
            result["reason"] = "original_question_partition_unresolved"
            result["missing"] = ["anchored_original_questions"]
            return result
        interpretation = {"id": "verbatim", "original_questions": originals, "derived_questions": [],
            "source_ids": [], "scope": [], "assumptions": [], "rationale": "Preserve the objective without inferred answers.",
            "confidence": "0"}
        interpretation = _interpretation(interpretation, raw, frozen, host)
        result["confidence_provenance"] = {"basis": "verbatim_bytes_only_not_semantic_quality", "value": None}
    else:
        document = _detach(proposal, "/proposal")
        _shape(document, proposal_schema(), "/proposal")
        candidates = [_interpretation(value, raw, frozen, host) for value in document["interpretations"]]
        if len({row["id"] for row in candidates}) != len(candidates):
            _refuse("proposal_interpretation_duplicate", "/proposal")
        ranked = sorted(enumerate(candidates), key=lambda row: (len(row[1]["derived_questions"]), row[0]))
        interpretation = ranked[0][1]
        result["interpretations"] = [{"id": row["id"], "rank": index + 1,
            "basis": "complete_original_coverage_then_fewest_derived_questions_then_supplied_order",
            "rationale": row["rationale"], "confidence": row["confidence"], "confidence_verified": False}
            for index, (_, row) in enumerate(ranked)]
        result["confidence_provenance"] = {"basis": "unverified_proposal_assertion", "value": interpretation["confidence"]}
    questions = [{"id": row["id"], "text": _span(raw, row["start_byte"], row["end_byte"])}
                 for row in interpretation["original_questions"]]
    if any(len(row["text"]) > 4096 for row in questions):
        result["reason"] = "original_question_exceeds_request_bound"
        return result
    if len(questions) + len(interpretation["derived_questions"]) > budgets["questions"]:
        _refuse("question_budget_exceeded", "/defaults/budgets")
    result["question_anchors"] = interpretation["original_questions"] + [
        {"id": row["id"], "original_ids": row["original_ids"], "anchors": row["anchors"]}
        for row in interpretation["derived_questions"]]
    result["assumptions"] = ["Constraints remain in the original text; semantic adequacy has not been evaluated.",
        "Only host-declared sources are listed; no source has been observed or acquired.", *interpretation["assumptions"]]
    payload = {"goal": text, "questions": questions, "derived_questions": [
        {key: row[key] for key in ("id", "text", "original_ids")} for row in interpretation["derived_questions"]],
        "target": host["target"], "outputs": effective["outputs"], "scope": host["scope"],
        "domain": host.get("domain", {"mode": "none", "pack": None, "rationale": "No domain pack was selected by the host."}),
        "sources": sources, "host_tools": host.get("host_tools", []), "authority": authority,
        "budgets": budgets, "assumptions": result["assumptions"], "open_decisions": [],
        "strict_evidence": {"mode": "strict", "assurance": evidence_policy["assurance"],
                            "policy_id": evidence_policy["policy_id"], "policy_revision": evidence_policy["revision"]}}
    request = {"schema_version": "2.0", "kind": "research_request", "request_id": request_id, "payload": payload}
    try:
        encode_document("onboarding/research_request/v2", request)
    except UsageError:
        _refuse("compiled_request_invalid", "/request")
    wrapped = {"schema_version": "evidence-research-setup/v1", "request": request, "decisions": {
        "language": effective["language"], "project_name": effective["project_name"], "mode": "strict",
        "policy": evidence_policy, "question_plans": host.get("question_plans", []),
        "source_requirements": host.get("source_requirements", [])}}
    from .planning_contracts import schema_document

    _shape(wrapped, schema_document("evidence-research-setup/v1"), "/request")
    result.update(status="compiled", reason="request_compiled_no_effects", request=wrapped)
    return _detach(result, "/result")
