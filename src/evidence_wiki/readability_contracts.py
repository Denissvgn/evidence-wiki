"""Closed field-only critic reports contain no edits, grants or approval."""

import hashlib
import json
import re
from copy import deepcopy

from ._pack_io import bounded
from .errors import EvidenceWikiError, UsageError
from .mutation_contracts import canonical, detached, digest
from .onboarding_contract import _bounded_nesting, _matches
from .research_journal_contracts import detached_record

INPUT = "research-readability-input/v1"
OUTPUT = "research-readability-issues/v1"
ISSUES = ("qualification_hidden", "scope_misleading", "inference_as_finding", "evidence_ambiguous",
    "unresolved_outcome_hidden", "limitation_hidden", "meaning_unclear")
MAX_INPUT = 524288
MAX_OUTPUT = 65536
MAX_FIELDS = 4096
CAPABILITIES = ("model", "objection.propose", "review.read")
SEALED_INPUT = "research-readability-sealed-input/v1"
PREPARATION = "research-readability-preparation/v1"
MAX_PERSISTED_CHARS = 65536
MAX_JOURNAL_BYTES = 262144
RESERVATION_REASONS = frozenset({"run_terminal", "preterminal_policy_not_eligible",
    "typed_bundled_critic_required", "model_grant_missing", "whole_job_original_scope_bound",
    "attempt_limit", "occupied_or_overrun_budget", "budget_exhausted", "current_brief_not_supported",
    "current_qualification_or_basis_unknown", "protected_issuer_required", "complete_worker_input_bound",
    "basis_changed_before_reservation"})


def refuse(reason):
    raise UsageError("READABILITY_REFUSED", "Readability operation refused.", details={"reason": reason}, recoverable=False)


def reservation_reason(error):
    """Retain a closed owner code without copying exception or caller prose."""
    if type(error) is not UsageError or error.error_code != "READABILITY_REFUSED":
        return None
    reason = error.details.get("reason") if type(error.details) is dict else None
    return reason if type(reason) is str and reason in RESERVATION_REASONS else None


def reservation_diagnostic(error):
    """Omit optional metadata entirely for legacy and unknown failures."""
    reason = reservation_reason(error)
    return {"owner_reason": reason} if reason is not None else {}


def _persist(value, schema):
    raw = canonical(value)
    text = raw.decode("utf-8")
    if len(text) > MAX_PERSISTED_CHARS:
        refuse("persistence_bound")
    return {"schema_version": schema, "canonical_json": text, "sha256": hashlib.sha256(raw).hexdigest()}


def _restore(value, schema):
    if (type(value) is not dict or set(value) != {"schema_version", "canonical_json", "sha256"}
            or value["schema_version"] != schema or type(value["canonical_json"]) is not str
            or not 0 < len(value["canonical_json"]) <= MAX_PERSISTED_CHARS
            or type(value["sha256"]) is not str or re.fullmatch("[a-f0-9]{64}", value["sha256"]) is None):
        refuse("persistence_invalid")
    try:
        raw = value["canonical_json"].encode("utf-8")
        if len(raw) > 1_048_576 or hashlib.sha256(raw).hexdigest() != value["sha256"]:
            refuse("persistence_invalid")
        _bounded_nesting(value["canonical_json"])
    except (UnicodeError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("persistence_invalid")
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                refuse("persistence_invalid")
            result[key] = child
        return result
    try:
        restored = json.loads(raw, object_pairs_hook=pairs,
            parse_constant=lambda *_: refuse("persistence_invalid"))
        if type(restored) is not dict or _persist(restored, schema) != value:
            refuse("persistence_invalid")
    except (ValueError, UnicodeError, RecursionError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("persistence_invalid")
    return restored


def stored_attempt(attempt, profile):
    """Only selected Readability input gets a shallow persistence representation."""
    value = attempt.document()
    if attempt.input_document.get("purpose") != "readability":
        return value
    if profile.readability is None or attempt.config_ref != profile.readability.config_ref:
        refuse("persistence_scope_invalid")
    profile.check_job_scope(attempt.config_ref, attempt.job, attempt.input_document)
    value["input"] = _persist(attempt.input_document, SEALED_INPUT)
    return detached(value)


def restored_input(document, profile):
    value = document["input"]
    if type(value) is not dict or value.get("schema_version") != SEALED_INPUT:
        return value
    if profile.readability is None or document["config_ref"] != profile.readability.config_ref:
        refuse("persistence_scope_invalid")
    restored = _restore(value, SEALED_INPUT)
    if set(restored) != {"purpose", "packet"} or restored["purpose"] != "readability":
        refuse("persistence_scope_invalid")
    from .readability_context import input_document
    input_document(restored["packet"], max_input_bytes=profile.readability.max_input_bytes)
    return restored


def stored_preparation(value, execution):
    """Input is encoded first, so the exact descriptor obeys original tree bounds."""
    if type(value) is not dict or set(value) != {"attempt_record", "probe_record", "predecessor_records_ref"}:
        refuse("persistence_invalid")
    row = value["attempt_record"]
    execution.attempt(row["attempt"])
    return _persist(value, PREPARATION)


def restored_preparation(value, execution):
    restored = _restore(value, PREPARATION)
    if type(restored) is not dict or set(restored) != {"attempt_record", "probe_record", "predecessor_records_ref"}:
        refuse("persistence_invalid")
    row = restored["attempt_record"]
    if (type(row) is not dict or set(row) != {"attempt", "reservation", "purpose", "original_id", "claim_id", "readability_parent"}
            or row["purpose"] != "readability" or row["claim_id"] is not None
            or type(row["original_id"]) is not str or not 1 <= len(row["original_id"]) <= 256):
        refuse("persistence_invalid")
    if (type(row["attempt"]) is not dict or set(row["attempt"]) != {"attempt_id", "nonce", "run_id", "binding_id",
            "profile_sha256", "config_ref", "job_sha256", "input_sha256", "job", "input"}):
        refuse("persistence_invalid")
    try:
        attempt = execution.attempt(row["attempt"])
    except (ValueError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("persistence_invalid")
    if stored_attempt(attempt, execution.profile) != row["attempt"]:
        refuse("persistence_invalid")
    parent = row["readability_parent"]
    if (type(parent) is not dict or set(parent) != {"basis_id", "substance_sha256", "binding", "order_seed", "plan_sha256", "sources"}
            or parent["basis_id"] != attempt.job["basis_id"]
            or parent["plan_sha256"] != execution.profile.readability.sha256
            or type(parent["order_seed"]) is not str or re.fullmatch("[a-f0-9]{64}", parent["order_seed"]) is None
            or row["original_id"] not in attempt.job["scope"]):
        refuse("persistence_invalid")
    binding = parent["binding"]
    if (type(binding) is not dict or set(binding) != {"context_id", "packet_sha256", "substance_sha256", "aliases", "field_targets"}
            or binding["context_id"] != attempt.input_document["packet"]["context_id"]
            or binding["packet_sha256"] != digest(attempt.input_document["packet"])
            or binding["substance_sha256"] != parent["substance_sha256"]):
        refuse("persistence_invalid")
    aliases, targets = binding["aliases"], binding["field_targets"]
    if type(aliases) is not dict or type(targets) is not dict or len(aliases) > 4096 or len(targets) > MAX_FIELDS:
        refuse("persistence_invalid")
    for token, entry in aliases.items():
        if (type(token) is not str or type(entry) is not dict or set(entry) != {"kind", "canonical_ref"}
                or type(entry["kind"]) is not str or entry["kind"] not in {"original", "question-id", "question", "claim", "source"}
                or type(entry["canonical_ref"]) is not str or not 1 <= len(entry["canonical_ref"]) <= 4096
                or re.fullmatch(re.escape(entry["kind"]) + "-[a-f0-9]{24}", token) is None):
            refuse("persistence_invalid")
    claim_ids = {entry["canonical_ref"] for entry in aliases.values() if entry["kind"] == "claim"}
    for pointer, claim_id in targets.items():
        if (type(pointer) is not str or pointer not in attempt.input_document["packet"]["field_manifest"]
                or type(claim_id) is not str or claim_id not in claim_ids):
            refuse("persistence_invalid")
    if type(parent["sources"]) is not list or len(parent["sources"]) > 32:
        refuse("persistence_invalid")
    source_ids = set()
    for source in parent["sources"]:
        if type(source) is not dict or set(source) != {"source_id", "record_sha256", "source_binding"}:
            refuse("persistence_invalid")
        source_binding = source["source_binding"]
        if (type(source["source_id"]) is not str or not 1 <= len(source["source_id"]) <= 256
                or source["source_id"] in source_ids or type(source_binding) is not dict
                or set(source_binding) != {"source_id", "extent_ref", "closure_id"}
                or source_binding["source_id"] != source["source_id"]
                or type(source_binding["extent_ref"]) is not str or not 1 <= len(source_binding["extent_ref"]) <= 4096):
            refuse("persistence_invalid")
        source_ids.add(source["source_id"])
        for identifier in (source["record_sha256"], source_binding["closure_id"]):
            if type(identifier) is not str or re.fullmatch("sha256:[a-f0-9]{64}", identifier) is None:
                refuse("persistence_invalid")
    expected_reservation = {"attempt_id": attempt.attempt_id, "job_id": attempt.job["id"], "controller_ref": "session-observer",
        "issued_at": attempt.job["issued_utc"], "reservation": {**attempt.job["budgets"], "seconds": str(attempt.job["timeout_seconds"])},
        "cost_unit": {"currency": execution.profile.currency, "pricing_ref": execution.profile.pricing_ref},
        "required_slots": ["dispatch", "result", "usage"]}
    from .execution_accounting import validate_document
    try:
        validate_document("attempt", row["reservation"])
    except (ValueError, UsageError):
        refuse("persistence_invalid")
    if row["reservation"] != expected_reservation:
        refuse("persistence_invalid")
    probe = restored["probe_record"]
    if (type(probe) is not dict or set(probe) != {"probe", "domain_ref"}
            or type(probe["probe"]) is not dict or set(probe["probe"]) != {"schema_version", "profile_sha256", "registry_sha256",
                "semantic_basis_id", "expires_utc", "observed_utc", "configuration", "reasons"}
            or probe["probe"]["schema_version"] != "research-readability-probe/v1"
            or probe["probe"]["profile_sha256"] != execution.profile.sha256
            or type(restored["predecessor_records_ref"]) is not str
            or re.fullmatch("sha256:[a-f0-9]{64}", restored["predecessor_records_ref"]) is None):
        refuse("persistence_invalid")
    from .agent_execution import _utc
    from .calibration_probes import configuration_document
    document = probe["probe"]
    try:
        if document["configuration"] is not None and configuration_document(document["configuration"]) != document["configuration"]:
            refuse("persistence_invalid")
        if not _utc(document["observed_utc"]) < _utc(document["expires_utc"]):
            refuse("persistence_invalid")
    except (ValueError, TypeError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("persistence_invalid")
    if (type(document["reasons"]) is not list or len(document["reasons"]) > 32
            or any(type(reason) is not str or re.fullmatch("[a-z][a-z0-9_]{0,95}", reason) is None for reason in document["reasons"])):
        refuse("persistence_invalid")
    for name in (document["profile_sha256"], document["registry_sha256"]):
        if type(name) is not str or re.fullmatch("[a-f0-9]{64}", name) is None:
            refuse("persistence_invalid")
    for name in (probe["domain_ref"], document["semantic_basis_id"], parent["substance_sha256"]):
        if type(name) is not str or re.fullmatch("sha256:[a-f0-9]{64}", name) is None:
            refuse("persistence_invalid")
    if stored_preparation(restored, execution) != value:
        refuse("persistence_invalid")
    return restored


def check_persisted_record(record):
    """Check the complete escaped native envelope, never just inner input bytes."""
    detached_record(record)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        refuse("persistence_bound")


def dispatch_request_document(value, attempt, profile, registry_sha256):
    """An inert closed request binds the service echo to this exact job."""
    fields = {"schema_version", "request_id", "attempt_id", "job_sha256", "input_sha256", "binding_id", "nonce",
        "qualification_purpose", "config_ref", "profile_sha256", "registry_sha256", "semantic_basis_id", "expires_utc"}
    try:
        bounded(value)
        from .agent_execution import _utc
        from .execution_session import child_id
        if (type(value) is not dict or set(value) != fields or value["schema_version"] != "research-readability-dispatch-request/v1"
                or value["request_id"] != child_id(attempt.attempt_id, "dispatch_configuration")
                or value["attempt_id"] != attempt.attempt_id or value["job_sha256"] != attempt.job_sha256
                or value["input_sha256"] != attempt.input_sha256 or value["binding_id"] != attempt.binding_id
                or value["qualification_purpose"] != "readability" or value["config_ref"] != profile.readability.config_ref
                or value["profile_sha256"] != profile.sha256 or value["registry_sha256"] != registry_sha256
                or type(value["nonce"]) is not str or re.fullmatch("[a-f0-9]{32}", value["nonce"]) is None
                or type(value["semantic_basis_id"]) is not str or re.fullmatch("sha256:[a-f0-9]{64}", value["semantic_basis_id"]) is None
                or _utc(value["expires_utc"]) > _utc(attempt.job["deadline_utc"])):
            refuse("dispatch_configuration_invalid")
        return detached(value)
    except (ValueError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("dispatch_configuration_invalid")


def dispatch_observation_document(value, attempt, profile, registry_sha256):
    """Decode a closed transport observation; this never authenticates it."""
    expected = {"schema_version", "attempt_id", "job_sha256", "input_sha256", "binding_id", "request_id", "nonce", "probe", "request"}
    try:
        bounded(value)
        if (type(value) is not dict or set(value) != expected or value["schema_version"] != "research-readability-dispatch-observation/v1"
                or value["attempt_id"] != attempt.attempt_id or value["job_sha256"] != attempt.job_sha256
                or value["input_sha256"] != attempt.input_sha256 or value["binding_id"] != attempt.binding_id
                or type(value["request_id"]) is not str or not 1 <= len(value["request_id"]) <= 96
                or type(value["nonce"]) is not str or re.fullmatch("[a-f0-9]{32}", value["nonce"]) is None):
            refuse("dispatch_configuration_invalid")
        probe = value["probe"]
        request = dispatch_request_document(value["request"], attempt, profile, registry_sha256)
        if (type(probe) is not dict or set(probe) != {"schema_version", "profile_sha256", "registry_sha256", "semantic_basis_id",
                "observed_utc", "expires_utc", "configuration", "reasons"}
                or probe["schema_version"] != "research-readability-probe/v1" or probe["profile_sha256"] != profile.sha256
                or probe["registry_sha256"] != registry_sha256 or probe["reasons"] != []
                or any(value[key] != request[key] for key in ("request_id", "nonce"))
                or any(probe[key] != request[key] for key in ("profile_sha256", "registry_sha256", "semantic_basis_id", "expires_utc"))
                or type(probe["semantic_basis_id"]) is not str or re.fullmatch("sha256:[a-f0-9]{64}", probe["semantic_basis_id"]) is None):
            refuse("dispatch_configuration_invalid")
        from .agent_execution import _utc
        from .calibration_probes import configuration_document
        if (configuration_document(probe["configuration"]) != probe["configuration"]
                or not 0 < (_utc(probe["expires_utc"]) - _utc(probe["observed_utc"])).total_seconds() <= profile.readability.probe_lifetime_seconds
                or _utc(probe["expires_utc"]) > _utc(attempt.job["deadline_utc"])):
            refuse("dispatch_configuration_invalid")
        return detached(value)
    except (ValueError, UsageError) as error:
        if isinstance(error, UsageError) and error.error_code == "READABILITY_REFUSED":
            raise
        refuse("dispatch_configuration_invalid")


def pointer_value(value, pointer):
    if type(pointer) is not str or not pointer.startswith("/") or len(pointer) > 1024:
        refuse("pointer_invalid")
    try:
        selected = value
        for token in pointer[1:].split("/"):
            if "~" in token.replace("~0", "").replace("~1", ""):
                refuse("pointer_invalid")
            token = token.replace("~1", "/").replace("~0", "~")
            if type(selected) is list:
                if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                    refuse("pointer_invalid")
                selected = selected[int(token)]
            elif type(selected) is dict:
                selected = selected[token]
            else:
                refuse("pointer_invalid")
        return selected
    except (KeyError, IndexError, TypeError, ValueError):
        refuse("pointer_missing")


def field_manifest(value):
    bounded(value)
    result = {}

    def visit(part, pointer):
        if type(part) is dict and part:
            for key, child in part.items():
                visit(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif type(part) is list and part:
            for index, child in enumerate(part):
                visit(child, pointer + "/" + str(index))
        else:
            if len(result) >= MAX_FIELDS or len(pointer) > 1024:
                refuse("field_manifest_bound")
            result[pointer] = digest(part)
    visit(value, "")
    return result


def _schemas(native=None):
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    ref = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$(?![\s\S])"}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    issue = obj(code={"enum": list(ISSUES)}, pointer={"type": "string", "minLength": 1, "maxLength": 1024}, field_sha256=hashed)
    output = obj(schema_version={"const": OUTPUT}, context_id=ref, input_sha256=hashed,
        state={"enum": ["issues", "abstain"]}, issues={"type": "array", "maxItems": 32, "items": issue})
    from .brief_contracts import BRIEF
    from .brief_contracts import _schemas as brief_schemas
    content = deepcopy(brief_schemas(native)[BRIEF]["properties"])
    original = content["original_outcomes"]["items"]
    questions = original["properties"]["questions"]["items"]
    questions["properties"].pop("current_checks")
    questions["required"].remove("current_checks")
    eligible = original["properties"]["eligible_questions"]["items"]
    for name in ("accepted", "status"):
        eligible["properties"].pop(name)
        eligible["required"].remove(name)
    brief = obj(**{key: content[key] for key in
        ("verdict", "original_outcomes", "global_blockers", "accounting_complete", "limitations")})
    input_shape = obj(schema_version={"const": INPUT}, context_id=ref, task_role={"const": "critic"},
        payload=obj(brief=brief, markdown={"type": "string", "maxLength": MAX_INPUT}),
        field_manifest={"type": "object", "maxProperties": MAX_FIELDS,
            "patternProperties": {r"^/.{0,1023}$(?![\s\S])": hashed}, "additionalProperties": False},
        instructions={"type": "string", "minLength": 1, "maxLength": 4096},
        output_contract={"const": deepcopy(output)})
    return {INPUT: input_shape, OUTPUT: output}


def schemas():
    return _schemas()


def output_document(value, packet, *, max_output_bytes=MAX_OUTPUT):
    try:
        bounded(value)
        _matches(value, schemas()[OUTPUT])
        if len(canonical(value)) > max_output_bytes or value["context_id"] != packet["context_id"] or value["input_sha256"] != digest(packet):
            refuse("output_binding_invalid")
        if value["state"] == "abstain" and value["issues"]:
            refuse("abstention_has_issues")
        seen = set()
        for issue in value["issues"]:
            key = (issue["code"], issue["pointer"])
            if key in seen or packet["field_manifest"].get(issue["pointer"]) != issue["field_sha256"]:
                refuse("issue_binding_invalid")
            if digest(pointer_value(packet["payload"], issue["pointer"])) != issue["field_sha256"]:
                refuse("issue_binding_invalid")
            seen.add(key)
        return detached(value)
    except UsageError as error:
        if error.error_code == "READABILITY_REFUSED":
            raise
        refuse("output_invalid")
    except (EvidenceWikiError, ValueError, KeyError, TypeError, SystemExit):
        refuse("output_invalid")
