"""Bounded immutable first-pass evidence projections with explicit blind spots.

Packets are inert. The parent retains canonical identity and source bindings and
must authenticate dispatch, check current ownership and apply native reviews.
Fresh packet identities do not prove separate model controllers or reasoning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ._review_dependency_json import detached as _native_copy
from ._review_dependency_json import digest as _native_digest
from ._review_dependency_wire import WIRE
from ._review_dependency_wire import pack as _pack_native
from .agent_execution import ExecutionError, _digest, _integer, _json, _ref
from .review_wire import decode_dependency_material

REVIEW_CONTEXT = "research-review-context/v1"
CONTEXT_BINDING = "research-review-context-binding/v1"
REVIEW_CONTEXT_V2 = "research-review-context/v2"
CONTEXT_BINDING_V2 = "research-review-context-binding/v2"
REVIEW_CONTEXT_V3 = "research-review-context/v3"
CONTEXT_BINDING_V3 = "research-review-context-binding/v3"
REVIEW_CONTEXT_V4 = "research-review-context/v4"
CONTEXT_BINDING_V4 = "research-review-context-binding/v4"
COMPLETE_REVIEW_MATERIAL = "research-complete-review-material/v1"
MAX_CONTEXT_BYTES = 262144
_HASH = re.compile(r"^sha256:[a-f0-9]{64}$")
_CLAIM_FIELDS = ("text", "scope", "time", "units", "derivation", "limitations")
_EVIDENCE_FIELDS = ("record_sha256", "observed_at", "capture", "quote", "location_hint", "anchor")
_METADATA_FIELDS = frozenset({"title", "published_at", "source_type", "url", "authors"})
_PACKET_FIELDS = frozenset({"schema_version", "context_id", "task_role", "basis_id", "rubric",
    "claim", "question", "premises", "sources", "computation", "manifest"})
_MANIFEST_FIELDS = frozenset({"required_source_ids", "included_source_ids", "missing_source_ids",
    "omitted_source_ids", "context_complete", "truncated", "limitations", "sections"})
_BINDING_FIELDS = frozenset({"schema_version", "context_id", "task_role", "basis_id", "packet_sha256",
    "owner_snapshot_sha256", "rubric_sha256", "source_input_sha256", "aliases", "source_permutation",
    "order_seed_sha256", "order_seed", "max_bytes"})
_CRITERIA = frozenset({"support", "source_suitability", "scope", "time", "units", "counterevidence"})
_LIMITATIONS = frozenset({"source_context_missing", "source_context_truncated", "premise_context_missing",
                        "premise_question_context_missing", "computation_context_unavailable"})
_NATIVE_CLAIM_FIELDS = {"id", "question_slug", "qualification", "premises", "evidence", *_CLAIM_FIELDS}
_QUESTION_FIELDS = {"slug", "original_id", "question", "metadata", "body"}
_QUALIFICATIONS = {"attributed", "supported", "inference", "contested", "insufficient_evidence"}


def _closure(claims: list[dict[str, Any]], subject: str) -> list[dict[str, Any]]:
    if type(claims) is not list or not 1 <= len(claims) <= 32 or any(type(row) is not dict for row in claims):
        raise ExecutionError("review_context_material_invalid")
    by_id = {_ref(row["id"]): row for row in claims}
    if len(by_id) != len(claims):
        raise ExecutionError("review_context_claim_duplicate")
    result, visited, active = [], set(), set()

    def visit(identifier: str) -> None:
        if identifier not in by_id or identifier in active:
            raise ExecutionError("review_context_material_closure_invalid")
        if identifier in visited:
            return
        visited.add(identifier)
        active.add(identifier)
        row = by_id[identifier]
        edges = row["premises"]
        if type(edges) is not list or len(edges) > 32 or any(type(edge) is not str for edge in edges) or len(set(edges)) != len(edges):
            raise ExecutionError("review_context_claim_invalid")
        result.append(row)
        for edge in edges:
            visit(_ref(edge))
        active.remove(identifier)

    visit(subject)
    if visited != set(by_id):
        raise ExecutionError("review_context_material_closure_invalid")
    return result


def _native_claim(row: dict[str, Any]) -> None:
    if (type(row) is not dict or set(row) not in (_NATIVE_CLAIM_FIELDS, _NATIVE_CLAIM_FIELDS | {"calculations"})
            or type(row["qualification"]) is not str or row["qualification"] not in _QUALIFICATIONS
            or type(row["premises"]) is not list or len(row["premises"]) > 32
            or any(type(edge) is not str for edge in row["premises"])
            or len(set(row["premises"])) != len(row["premises"])
            or type(row["evidence"]) is not list or len(row["evidence"]) > 16):
        raise ExecutionError("review_context_claim_invalid")
    if row["qualification"] == "inference":
        if not row["premises"] or type(row["derivation"]) is not str:
            raise ExecutionError("review_context_claim_invalid")
    elif row["premises"] or row["derivation"] is not None:
        raise ExecutionError("review_context_claim_invalid")
    if row.get("calculations") and row["qualification"] != "inference":
        raise ExecutionError("review_context_claim_invalid")
    if "calculations" in row:
        if type(row["calculations"]) is not list or len(row["calculations"]) > 16:
            raise ExecutionError("review_context_claim_invalid")
        for reference in row["calculations"]:
            if (type(reference) is not dict or set(reference) != {"result_id", "pointer", "expected", "form", "unit", "rounded"}
                    or type(reference["form"]) is not str or reference["form"] not in {"value", "formatted"}
                    or type(reference["rounded"]) is not bool):
                raise ExecutionError("review_context_claim_invalid")
            _hash(reference["result_id"])
            pointer = _text(reference["pointer"], 512)
            if re.fullmatch(r"/(graphs/[A-Za-z][A-Za-z0-9_]*/outputs/[A-Za-z][A-Za-z0-9_]*|aggregations/[A-Za-z][A-Za-z0-9_]*/groups/[0-9]+/metrics/[A-Za-z][A-Za-z0-9_]*)", pointer) is None:
                raise ExecutionError("review_context_claim_invalid")
            _text(reference["expected"], 512)
            _text(reference["unit"], 128)
    for evidence in row["evidence"]:
        if (type(evidence) is not dict or set(evidence) != {"source_id", *_EVIDENCE_FIELDS}
                or (evidence["quote"] is None) == (evidence["anchor"] is None)
                or (evidence["anchor"] is not None and evidence["location_hint"] is not None)):
            raise ExecutionError("review_context_claim_invalid")


def _questions(rows: Any, slugs: set[str]) -> None:
    if type(rows) is not list or not 1 <= len(rows) <= 32:
        raise ExecutionError("review_context_question_invalid")
    found = []
    for row in rows:
        if type(row) is not dict or set(row) != _QUESTION_FIELDS:
            raise ExecutionError("review_context_question_invalid")
        found.append(_ref(row["slug"]))
        _ref(row["original_id"])
        _text(row["question"])
        if row["metadata"] is not None and type(row["metadata"]) is not dict:
            raise ExecutionError("review_context_question_invalid")
        if type(row["body"]) is not str or len(row["body"]) > 65536:
            raise ExecutionError("review_context_question_invalid")
    if len(found) != len(set(found)) or set(found) != slugs:
        raise ExecutionError("review_context_question_coverage_invalid")


def _complete_material(material: Any, snapshot: dict[str, Any], basis: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if (type(material) is not dict or set(material) != {"schema_version", "basis_id", "native_snapshot_sha256", "claim_id", "claims", "questions"}
            or material["schema_version"] != COMPLETE_REVIEW_MATERIAL or material["basis_id"] != basis
            or material["native_snapshot_sha256"] != "sha256:" + _digest(snapshot)
            or material["claim_id"] != snapshot["claim"]["id"]):
        raise ExecutionError("review_context_material_invalid")
    claims = _closure(material["claims"], material["claim_id"])
    if claims != material["claims"]:
        raise ExecutionError("review_context_material_closure_invalid")
    for row in claims:
        _native_claim(row)
    by_id = {row["id"]: row for row in claims}
    if any(by_id.get(row["id"]) != row for row in [snapshot["claim"], *snapshot["premises"]]):
        raise ExecutionError("review_context_material_invalid")
    if snapshot["premises"] != [by_id[identifier] for identifier in snapshot["claim"]["premises"]]:
        raise ExecutionError("review_context_material_invalid")
    questions = material["questions"]
    _questions(questions, {row["question_slug"] for row in claims})
    question = next(row for row in questions if row["slug"] == snapshot["question"]["slug"])
    if any(question[name] != value for name, value in snapshot["question"].items()):
        raise ExecutionError("review_context_material_invalid")
    return claims, questions


def _projection(packet: dict[str, Any]) -> None:
    if packet.get("schema_version") == REVIEW_CONTEXT_V4:
        if "dependency_material" not in packet:
            raise ExecutionError("review_context_material_invalid")
        decode_dependency_material(packet["dependency_material"])
        legacy = {key: value for key, value in packet.items() if key != "dependency_material"}
        legacy["schema_version"] = REVIEW_CONTEXT_V3
        _projection(legacy)
        return
    rubric = packet["rubric"]
    if (type(rubric) is not dict or set(rubric) != {"id", "revision", "criteria"}
            or type(rubric["criteria"]) is not dict or set(rubric["criteria"]) != _CRITERIA):
        raise ExecutionError("review_context_rubric_invalid")
    _ref(rubric["id"])
    _ref(rubric["revision"])
    for value in rubric["criteria"].values():
        _text(value)
    question = packet["question"]
    if (type(question) is not dict or set(question) != {"slug", "original_id", "question"}
            or question["slug"] != "question-1" or question["original_id"] != "original-1"):
        raise ExecutionError("review_context_question_invalid")
    _text(question["question"])
    if type(packet["premises"]) is not list or len(packet["premises"]) > 32 or (packet["schema_version"] == REVIEW_CONTEXT and packet["computation"] is not None):
        raise ExecutionError("review_context_inputs_invalid")
    complete = packet["schema_version"] == REVIEW_CONTEXT_V3
    computed = packet["schema_version"] == REVIEW_CONTEXT_V2 or (complete and packet["computation"] is not None)
    if computed:
        from .calculation_context import validate_context
        validate_context(packet["computation"])
    for claim in [packet["claim"], *packet["premises"]]:
        if type(claim) is not dict:
            raise ExecutionError("review_context_claim_invalid")
        if complete:
            native = {key: value for key, value in claim.items() if key != "kind"}
            _native_claim(native)
            if claim.get("kind") != ("inference" if native["qualification"] == "inference" else "assertion"):
                raise ExecutionError("review_context_claim_invalid")
        fields = {"id", "question_slug", "kind", "premises", "evidence", *_CLAIM_FIELDS}
        fields |= ({"qualification"} | ({"calculations"} if computed or "calculations" in claim else set())) if complete else ({"calculations"} if computed else set())
        if (type(claim) is not dict or set(claim) != fields
                or type(claim["id"]) is not str or re.fullmatch(r"claim-[1-9][0-9]{0,3}", claim["id"]) is None
                or type(claim["question_slug"]) is not str or re.fullmatch(r"question-[1-9][0-9]?", claim["question_slug"]) is None
                or claim["kind"] not in {"assertion", "inference"}
                or type(claim["premises"]) is not list or len(claim["premises"]) > 32
                or any(type(item) is not str or re.fullmatch(r"claim-[1-9][0-9]{0,3}", item) is None for item in claim["premises"])
                or type(claim["evidence"]) is not list or len(claim["evidence"]) > 16):
            raise ExecutionError("review_context_claim_invalid")
        if computed:
            from .calculation_context import validate_references
            validate_references(claim["calculations"], packet["computation"])
        for name in _CLAIM_FIELDS:
            if name == "limitations":
                if type(claim[name]) is not list or len(claim[name]) > 16:
                    raise ExecutionError("review_context_claim_invalid")
                for item in claim[name]:
                    _text(item)
            else:
                _text(claim[name], nullable=name == "derivation")
        for item in claim["evidence"]:
            if type(item) is not dict or set(item) != {"source_id", *_EVIDENCE_FIELDS}:
                raise ExecutionError("review_context_claim_invalid")
            _hash(item["record_sha256"])
            _text(item["observed_at"])
            _text(item["quote"], nullable=True)
            _text(item["location_hint"], nullable=True)
            if type(item["capture"]) is not str or item["capture"] not in {"primary", "excerpt", "summary", "unknown"}:
                raise ExecutionError("review_context_capture_invalid")
            anchor = item["anchor"]
            if anchor is not None:
                if type(anchor) is not dict or set(anchor) != {"pointer", "expected"}:
                    raise ExecutionError("review_context_anchor_invalid")
                for value in anchor.values():
                    _text(value, 512)
    if type(packet["sources"]) is not list or len(packet["sources"]) > 64:
        raise ExecutionError("review_context_source_scope_invalid")
    for source in packet["sources"]:
        if (type(source) is not dict or set(source) != {"source_id", "source_revision", "record_sha256", "metadata", "content"}
                or type(source["metadata"]) is not dict or not set(source["metadata"]) <= _METADATA_FIELDS):
            raise ExecutionError("review_context_source_scope_invalid")
        _hash(source["source_revision"])
        _hash(source["record_sha256"])
        _metadata(source["metadata"])
        _text(source["content"])


def _hash(value: Any) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ExecutionError("review_context_digest_invalid")
    return value


def _text(value: Any, maximum: int = 65536, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ExecutionError("review_context_text_invalid")
    return value


def _metadata(supplied: Any) -> dict[str, Any]:
    if type(supplied) is not dict:
        raise ExecutionError("review_context_metadata_invalid")
    result = {}
    for name in _METADATA_FIELDS.intersection(supplied):
        value = supplied[name]
        if value is None:
            continue
        if name == "authors":
            if type(value) is not list or len(value) > 16:
                raise ExecutionError("review_context_metadata_invalid")
            result[name] = [_text(item, 256) for item in value]
        else:
            result[name] = _text(value, 4096)
    return result


def _claim(supplied: dict[str, Any], claim_aliases: dict[str, str], source_aliases: dict[str, str],
           question_aliases: dict[str, str]) -> dict[str, Any]:
    result = {"id": claim_aliases[supplied["id"]], "question_slug": question_aliases[supplied["question_slug"]],
              "kind": "inference" if supplied.get("qualification") == "inference" else "assertion"}
    for name in _CLAIM_FIELDS:
        value = supplied.get(name)
        if name == "limitations":
            if type(value) is not list or len(value) > 16:
                raise ExecutionError("review_context_claim_invalid")
            result[name] = [_text(item) for item in value]
        else:
            result[name] = _text(value, nullable=name == "derivation")
    result["premises"] = [claim_aliases[_ref(item)] for item in supplied["premises"]]
    result["evidence"] = []
    evidence = supplied.get("evidence")
    if type(evidence) is not list or len(evidence) > 16:
        raise ExecutionError("review_context_claim_invalid")
    for item in evidence:
        if type(item) is not dict:
            raise ExecutionError("review_context_claim_invalid")
        row = {"source_id": source_aliases[_ref(item["source_id"])]}
        for name in _EVIDENCE_FIELDS:
            value = item.get(name)
            if name == "record_sha256":
                row[name] = _hash(value)
            elif name == "anchor":
                if value is not None:
                    if type(value) is not dict or set(value) != {"pointer", "expected"}:
                        raise ExecutionError("review_context_anchor_invalid")
                    value = {key: _text(part, 512) for key, part in value.items()}
                row[name] = value
            elif name == "capture":
                if type(value) is not str or value not in {"primary", "excerpt", "summary", "unknown"}:
                    raise ExecutionError("review_context_capture_invalid")
                row[name] = value
            else:
                row[name] = _text(value, nullable=name in {"quote", "location_hint"})
        result["evidence"].append(row)
    return result


@dataclass(frozen=True)
class ReviewContext:
    """Detached worker packet and parent-only binding, without signing authority."""

    _packet_bytes: bytes = field(repr=False)
    _binding_bytes: bytes = field(repr=False)

    def __post_init__(self) -> None:
        try:
            self._validate()
        except (KeyError, TypeError, IndexError, AttributeError, StopIteration):
            raise ExecutionError("review_context_invalid") from None

    def _validate(self) -> None:
        if type(self._packet_bytes) is not bytes or type(self._binding_bytes) is not bytes:
            raise ExecutionError("review_context_invalid")
        try:
            packet, binding = json.loads(self._packet_bytes), json.loads(self._binding_bytes)
        except (ValueError, UnicodeError, RecursionError):
            raise ExecutionError("review_context_invalid") from None
        dependencies = type(packet) is dict and packet.get("schema_version") == REVIEW_CONTEXT_V4
        if dependencies:
            try:
                _validate_dependency_context(packet, binding, self._packet_bytes, self._binding_bytes)
            except ExecutionError:
                raise
            except (ValueError, UnicodeError, OverflowError):
                raise ExecutionError("review_context_material_invalid") from None
            return
        complete = type(packet) is dict and packet.get("schema_version") == REVIEW_CONTEXT_V3
        if (type(packet) is not dict or set(packet) != _PACKET_FIELDS | ({"questions"} if complete else set())
                or packet["schema_version"] not in {REVIEW_CONTEXT, REVIEW_CONTEXT_V2, REVIEW_CONTEXT_V3} or _json(packet) != self._packet_bytes
                or type(binding) is not dict or set(binding) != _BINDING_FIELDS | ({"complete_material_sha256"} if complete else set())
                or binding["schema_version"] != (CONTEXT_BINDING_V3 if complete else CONTEXT_BINDING_V2 if packet["schema_version"] == REVIEW_CONTEXT_V2 else CONTEXT_BINDING) or _json(binding) != self._binding_bytes
                or len(self._packet_bytes) > MAX_CONTEXT_BYTES):
            raise ExecutionError("review_context_invalid")
        _ref(packet["context_id"])
        _hash(packet["basis_id"])
        if type(packet["task_role"]) is not str or packet["task_role"] not in {"evaluator", "critic", "adjudicator"}:
            raise ExecutionError("review_context_role_invalid")
        if any(binding[name] != packet[name] for name in ("context_id", "task_role", "basis_id")):
            raise ExecutionError("review_context_binding_mismatch")
        if binding["packet_sha256"] != "sha256:" + _digest(packet) or binding["rubric_sha256"] != "sha256:" + _digest(packet["rubric"]):
            raise ExecutionError("review_context_binding_mismatch")
        for name in ("owner_snapshot_sha256", "rubric_sha256", "source_input_sha256", "order_seed_sha256"):
            _hash(binding[name])
        if complete:
            _hash(binding["complete_material_sha256"])
        seed = binding["order_seed"]
        if type(seed) is not str or re.fullmatch(r"[a-f0-9]{64,256}", seed) is None or len(seed) % 2:
            raise ExecutionError("review_context_order_seed_invalid")
        if "sha256:" + hashlib.sha256(bytes.fromhex(seed)).hexdigest() != binding["order_seed_sha256"]:
            raise ExecutionError("review_context_binding_mismatch")
        if not 4096 <= _integer(binding["max_bytes"], MAX_CONTEXT_BYTES) or len(self._packet_bytes) > binding["max_bytes"]:
            raise ExecutionError("review_context_bound_invalid")
        _projection(packet)
        manifest = packet["manifest"]
        if type(manifest) is not dict or set(manifest) != _MANIFEST_FIELDS:
            raise ExecutionError("review_context_manifest_invalid")
        for name in ("context_complete", "truncated"):
            if type(manifest[name]) is not bool:
                raise ExecutionError("review_context_manifest_invalid")
        required, included, missing, omitted = [manifest[name] for name in
            ("required_source_ids", "included_source_ids", "missing_source_ids", "omitted_source_ids")]
        for ids in (required, included, missing, omitted):
            if type(ids) is not list or len(ids) > 64 or any(type(item) is not str for item in ids) or len(set(ids)) != len(ids):
                raise ExecutionError("review_context_manifest_invalid")
        if (type(manifest["limitations"]) is not list or any(type(item) is not str for item in manifest["limitations"])
                or len(set(manifest["limitations"])) != len(manifest["limitations"])
                or not set(manifest["limitations"]) <= _LIMITATIONS
                or any(re.fullmatch(r"source-[1-9][0-9]?", item) is None for item in required)):
            raise ExecutionError("review_context_manifest_invalid")
        references = {item["source_id"] for claim in [packet["claim"], *packet["premises"]] for item in claim["evidence"]}
        if packet["computation"] is not None:
            references.update(row["source_id"] for row in packet["computation"]["sources"])
        if references != set(required):
            raise ExecutionError("review_context_manifest_invalid")
        claims = [packet["claim"], *packet["premises"]]
        claim_ids = {claim["id"] for claim in claims}
        unresolved_premises = {identifier for claim in claims for identifier in claim["premises"]} - claim_ids
        other_questions = any(claim["question_slug"] != "question-1" for claim in claims)
        if complete:
            if _closure(claims, packet["claim"]["id"]) != claims:
                raise ExecutionError("review_context_material_closure_invalid")
            _questions(packet["questions"], {claim["question_slug"] for claim in claims})
            primary = next(row for row in packet["questions"] if row["slug"] == "question-1")
            if any(primary[name] != value for name, value in packet["question"].items()):
                raise ExecutionError("review_context_question_invalid")
        if (len(claim_ids) != len(claims) or packet["claim"]["question_slug"] != "question-1"
                or ("source_context_missing" in manifest["limitations"]) != bool(missing)
                or ("source_context_truncated" in manifest["limitations"]) != bool(omitted)
                or ("premise_context_missing" in manifest["limitations"]) != bool(unresolved_premises)
                or ("premise_question_context_missing" in manifest["limitations"]) != (other_questions and not complete)):
            raise ExecutionError("review_context_manifest_invalid")
        if (set(required) != set(included) | set(missing) | set(omitted)
                or set(included).intersection(missing) or set(included).intersection(omitted) or set(missing).intersection(omitted)
                or type(packet["sources"]) is not list
                or [row.get("source_id") for row in packet["sources"] if type(row) is dict] != included
                or manifest["truncated"] != bool(omitted)
                or manifest["context_complete"] != (not missing and not omitted and not manifest["limitations"])):
            raise ExecutionError("review_context_manifest_invalid")
        sections = manifest["sections"]
        if type(sections) is not list or len(sections) > 64:
            raise ExecutionError("review_context_manifest_invalid")
        for section in sections:
            if (type(section) is not dict or set(section) != {"section_id", "sha256", "bytes", "included", "required"}
                    or section["section_id"] not in set(included) | set(omitted)
                    or type(section["included"]) is not bool or section["included"] != (section["section_id"] in included)
                    or section["required"] is not True):
                raise ExecutionError("review_context_manifest_invalid")
            _hash(section["sha256"])
            _integer(section["bytes"], 1048576)
            if section["included"]:
                source = next(row for row in packet["sources"] if row["source_id"] == section["section_id"])
                if section["sha256"] != "sha256:" + _digest(source) or section["bytes"] != len(_json(source)):
                    raise ExecutionError("review_context_manifest_invalid")
        if {section["section_id"] for section in sections} != set(included) | set(omitted) or len(sections) != len(included) + len(omitted):
            raise ExecutionError("review_context_manifest_invalid")

    def document(self) -> dict[str, Any]:
        return json.loads(self._packet_bytes)

    def binding(self) -> dict[str, Any]:
        return json.loads(self._binding_bytes)

    def check_owner(self, *, owner_snapshot: dict[str, Any], basis_id: str) -> None:
        binding = self.binding()
        snapshot_digest = (_native_digest(owner_snapshot) if binding["schema_version"] == CONTEXT_BINDING_V4
            else "sha256:" + _digest(owner_snapshot))
        if _hash(basis_id) != binding["basis_id"] or snapshot_digest != binding["owner_snapshot_sha256"]:
            raise ExecutionError("review_context_owner_changed")

    def check(self, owner_snapshot: dict[str, Any], rubric: dict[str, Any], sources: list[dict[str, Any]], *,
              complete_material: dict[str, Any] | None = None,
              dependency_material: dict[str, Any] | None = None) -> None:
        """Reconstruct the exact current projection against retained parent inputs."""
        binding = self.binding()
        if (binding["schema_version"] == CONTEXT_BINDING_V4) != (dependency_material is not None):
            raise ExecutionError("review_context_owner_changed")
        if (binding["schema_version"] == CONTEXT_BINDING_V3) != (complete_material is not None):
            raise ExecutionError("review_context_owner_changed")
        current = create_review_context(context_id=binding["context_id"], task_role=binding["task_role"],
            basis_id=binding["basis_id"], owner_snapshot=owner_snapshot, rubric=rubric, sources=sources,
            order_seed=bytes.fromhex(binding["order_seed"]), max_bytes=binding["max_bytes"], complete_material=complete_material,
            dependency_material=dependency_material)
        if dependency_material is not None and self.document()["dependency_material"].get("schema_version") != WIRE:
            packet, current_binding = current.document(), current.binding()
            packet["dependency_material"] = decode_dependency_material(packet["dependency_material"])
            current_binding["packet_sha256"] = "sha256:" + _digest(packet)
            current = ReviewContext(_json(packet), _json(current_binding))
        if current._packet_bytes != self._packet_bytes or current._binding_bytes != self._binding_bytes:
            raise ExecutionError("review_context_owner_changed")

    @classmethod
    def restore(cls, packet: dict[str, Any], binding: dict[str, Any]) -> ReviewContext:
        """Decode retained inert context; its owner must authenticate the journal."""
        return cls(_json(packet), _json(binding))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._packet_bytes).hexdigest()

    @property
    def packet_id(self) -> str:
        return "sha256:" + self.sha256


def create_review_context(*, context_id: str, task_role: str, basis_id: str, owner_snapshot: dict[str, Any],
                          rubric: dict[str, Any], sources: list[dict[str, Any]], order_seed: bytes,
                          max_bytes: int = MAX_CONTEXT_BYTES,
                          complete_material: dict[str, Any] | None = None,
                          dependency_material: dict[str, Any] | None = None) -> ReviewContext:
    """Project complete owner inputs or explicitly disclose missing decisive context.

    Source contents remain exact inert evidence. Only control-plane metadata and
    canonical subject names are masked; scientifically relevant text is retained.
    Complete material adds authenticated graph and question context while keeping
    the native owner snapshot unchanged. Computation is qualified independently.
    """
    if dependency_material is not None:
        if complete_material is not None:
            raise ExecutionError("review_context_material_invalid")
        return _create_dependency_context(context_id=context_id, task_role=task_role, basis_id=basis_id,
            owner_snapshot=owner_snapshot, rubric=rubric, sources=sources, order_seed=order_seed,
            max_bytes=max_bytes, material=dependency_material)
    _ref(context_id)
    _hash(basis_id)
    if type(task_role) is not str or task_role not in {"evaluator", "critic", "adjudicator"}:
        raise ExecutionError("review_context_role_invalid")
    if type(order_seed) is not bytes or not 32 <= len(order_seed) <= 128:
        raise ExecutionError("review_context_order_seed_invalid")
    if not 4096 <= _integer(max_bytes, MAX_CONTEXT_BYTES):
        raise ExecutionError("review_context_bound_invalid")
    if type(owner_snapshot) is not dict or type(rubric) is not dict or type(sources) is not list or len(sources) > 64:
        raise ExecutionError("review_context_inputs_invalid")
    _json(owner_snapshot)
    _json(sources)
    if complete_material is not None:
        _json(complete_material)
    if set(rubric) != {"id", "revision", "criteria"} or type(rubric["criteria"]) is not dict or set(rubric["criteria"]) != _CRITERIA:
        raise ExecutionError("review_context_rubric_invalid")
    projected_rubric = {"id": _ref(rubric["id"]), "revision": _ref(rubric["revision"]),
                        "criteria": {_ref(key): _text(value) for key, value in rubric["criteria"].items()}}
    try:
        question, subject = owner_snapshot["question"], owner_snapshot["claim"]
        premises = owner_snapshot["premises"]
        revisions = owner_snapshot["source_revisions"]
        if type(subject) is not dict or type(question) is not dict or type(premises) is not list or len(premises) > 32 or type(revisions) is not dict:
            raise ExecutionError("review_context_inputs_invalid")
        claims = [subject, *premises]
        complete = complete_material is not None
        questions = None
        if complete:
            claims, questions = _complete_material(complete_material, owner_snapshot, basis_id)
        computation = owner_snapshot.get("computation")
        computed = computation is not None
        _ref(question["slug"])
        _ref(question["original_id"])
        if subject["question_slug"] != question["slug"]:
            raise ExecutionError("review_context_question_mismatch")
        claim_ids = [_ref(claim["id"]) for claim in claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ExecutionError("review_context_claim_duplicate")
        premise_ids, source_ids = set(), set()
        for claim in claims:
            if type(claim["premises"]) is not list or len(claim["premises"]) > 32 or len(set(claim["premises"])) != len(claim["premises"]):
                raise ExecutionError("review_context_claim_invalid")
            _ref(claim["question_slug"])
            premise_ids.update(_ref(item) for item in claim["premises"])
            source_ids.update(_ref(item["source_id"]) for item in claim["evidence"])
        if computed:
            source_ids.update(row["source_id"] for row in computation["sources"])
        if len(source_ids) > 64 or len(premise_ids) > 1024:
            raise ExecutionError("review_context_bound_invalid")
        claim_aliases = {identifier: f"claim-{index + 1}" for index, identifier in enumerate(claim_ids + sorted(premise_ids - set(claim_ids)))}
        source_aliases = {identifier: f"source-{index + 1}" for index, identifier in enumerate(sorted(source_ids))}
        question_ids = [question["slug"], *sorted({claim["question_slug"] for claim in claims} - {question["slug"]})]
        question_aliases = {identifier: f"question-{index + 1}" for index, identifier in enumerate(question_ids)}
        projected_claims = [_claim(claim, claim_aliases, source_aliases, question_aliases) for claim in claims]
        if complete:
            for projected, claim in zip(projected_claims, claims, strict=True):
                projected["qualification"] = claim["qualification"]
                if "calculations" in claim:
                    projected["calculations"] = json.loads(_json(claim["calculations"]))
        if computed:
            for projected, claim in zip(projected_claims, claims, strict=True):
                projected["calculations"] = json.loads(_json(claim.get("calculations", [])))
            from .calculation_context import project
            computation = project(computation, source_aliases)
        projected_sources, seen, missing = [], set(), []
        for source in sources:
            identifier = _ref(source["source_id"])
            if identifier not in source_ids or identifier in seen:
                raise ExecutionError("review_context_source_scope_invalid")
            seen.add(identifier)
            revision = _hash(revisions.get(identifier))
            record = _hash(source["record_sha256"])
            if any(item["record_sha256"] != record for claim in claims for item in claim["evidence"] if item["source_id"] == identifier):
                raise ExecutionError("review_context_source_revision_mismatch")
            projected_sources.append({"source_id": source_aliases[identifier], "source_revision": revision,
                "record_sha256": record, "metadata": _metadata(source.get("metadata", {})),
                "content": _text(source["content"])})
        missing = sorted(source_aliases[identifier] for identifier in source_ids - seen)
        projected_sources.sort(key=lambda row: hmac.new(order_seed,
            (context_id + ":" + row["source_id"]).encode(), hashlib.sha256).digest())
        limitations = []
        if missing:
            limitations.append("source_context_missing")
        if premise_ids - set(claim_ids):
            limitations.append("premise_context_missing")
        if len(question_ids) > 1 and not complete:
            limitations.append("premise_question_context_missing")
        if not computed and any(claim.get("calculations") for claim in claims):
            limitations.append("computation_context_unavailable")
        packet = {"schema_version": REVIEW_CONTEXT_V3 if complete else REVIEW_CONTEXT_V2 if computed else REVIEW_CONTEXT, "context_id": context_id, "task_role": task_role,
            "basis_id": basis_id, "rubric": projected_rubric, "claim": projected_claims[0],
            "question": {"slug": "question-1", "original_id": "original-1", "question": _text(question["question"])},
            "premises": projected_claims[1:], "sources": projected_sources, "computation": computation,
            "manifest": {"required_source_ids": sorted(source_aliases.values()),
                "included_source_ids": [row["source_id"] for row in projected_sources], "missing_source_ids": missing,
                "omitted_source_ids": [], "context_complete": not limitations, "truncated": False,
                "limitations": limitations, "sections": []}}
        original_aliases = {question["original_id"]: "original-1"}
        if complete:
            originals = [question["original_id"], *sorted({row["original_id"] for row in questions} - {question["original_id"]})]
            original_aliases = {identifier: f"original-{index + 1}" for index, identifier in enumerate(originals)}
            packet["questions"] = [{**row, "slug": question_aliases[row["slug"]],
                "original_id": original_aliases[row["original_id"]]} for row in questions]
        manifest = packet["manifest"]
        sections = [{"section_id": row["source_id"], "sha256": "sha256:" + _digest(row),
            "bytes": len(_json(row)), "included": True, "required": True} for row in projected_sources]
        manifest["sections"] = sections
        permutation = [row["source_id"] for row in projected_sources]
        while len(_json(packet)) > max_bytes and packet["sources"]:
            removed = packet["sources"].pop()
            manifest["included_source_ids"].remove(removed["source_id"])
            manifest["omitted_source_ids"].append(removed["source_id"])
            manifest.update(truncated=True, context_complete=False)
            if "source_context_truncated" not in limitations:
                limitations.append("source_context_truncated")
            next(item for item in sections if item["section_id"] == removed["source_id"])["included"] = False
        if len(_json(packet)) > max_bytes:
            raise ExecutionError("review_context_bound_invalid")
        binding = {"schema_version": CONTEXT_BINDING_V3 if complete else CONTEXT_BINDING_V2 if computed else CONTEXT_BINDING, "context_id": context_id, "task_role": task_role,
            "basis_id": basis_id, "packet_sha256": "sha256:" + _digest(packet),
            "owner_snapshot_sha256": "sha256:" + _digest(owner_snapshot), "rubric_sha256": "sha256:" + _digest(projected_rubric),
            "source_input_sha256": "sha256:" + _digest(sources), "max_bytes": max_bytes,
            "aliases": {"claims": claim_aliases, "sources": source_aliases,
                "question": question_aliases, "original": original_aliases},
            "source_permutation": permutation, "order_seed_sha256": "sha256:" + hashlib.sha256(order_seed).hexdigest(),
            "order_seed": order_seed.hex()}
        if complete:
            binding["complete_material_sha256"] = "sha256:" + _digest(complete_material)
        return ReviewContext(_json(packet), _json(binding))
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ExecutionError("review_context_inputs_invalid") from None


def _dependency_contract():
    from .pack_discovery import owner
    return owner("_review_dependency_contract")


def _checked_dependency_material(material):
    try:
        return _dependency_contract().validate_material(material)
    except (ValueError, UnicodeError, OverflowError):
        raise ExecutionError("review_context_material_invalid") from None


def _dependency_aliases(material, base):
    from .pack_discovery import owner
    typed_value = owner("_question_dependencies").typed_value
    aliases = json.loads(_json(base))
    all_claims, all_questions, all_sources = set(aliases["claims"]), set(aliases["question"]), set(aliases["sources"])
    for row in material["global_material"]["material"]["global_dependencies"]:
        if row["kind"] == "claims_inventory":
            all_claims.update(row["value"]["ids"])
        elif row["kind"] == "questions_inventory":
            all_questions.update(row["value"]["ids"])
        elif row["kind"] == "source_inventory":
            all_sources.update(source["source_id"] for source in row["value"]["sources"])
        elif row["kind"] == "source_record":
            all_sources.update(source["source_id"] for source in row["value"]["ancestry"])
    for name, values, prefix in (("claims", all_claims, "claim-"), ("question", all_questions, "question-"),
            ("sources", all_sources, "source-")):
        for value in sorted(values - set(aliases[name]), key=lambda value: value.encode("utf-8")):
            aliases[name][value] = prefix + str(len(aliases[name]) + 1)
    aliases["facets"] = {}
    for question in material["local_context"]["counterevidence"]["questions"]:
        for criterion in typed_value(question["criteria"]):
            coordinate = question["question_slug"] + ":" + criterion["facet_id"]
            if coordinate not in aliases["facets"]:
                aliases["facets"][coordinate] = "facet-" + str(len(aliases["facets"]) + 1)
    aliases["paths"] = {}
    for index, question in enumerate(material["questions"]):
        aliases["paths"][question["path"]] = "questions/question-" + str(index + 1) + ".md"
        mirrors = question["canonical_mirrors"]
        slug = question["manifest_question"]["slug"]
        aliases["paths"].setdefault(mirrors["answer_page"], aliases["question"][slug] + ".md")
        aliases["paths"].setdefault(mirrors["coverage_manifest"], "coverage/" + aliases["question"][slug] + ".yml")
    return aliases


def _translate_dependency_material(material, aliases, *, inverse=False):
    """Translate schema-owned coordinates; arbitrary semantic strings stay exact."""
    value = _native_copy(material)
    from .pack_discovery import owner
    typed = owner("_question_dependencies")
    maps = {name: ({alias: identifier for identifier, alias in mapping.items()} if inverse else mapping)
        for name, mapping in aliases.items()}
    if any(len(set(mapping.values())) != len(mapping) for mapping in aliases.values()):
        raise ExecutionError("review_context_alias_collision")

    def coordinate(name, identifier):
        if identifier not in maps[name]:
            raise ExecutionError("review_context_alias_missing")
        return maps[name][identifier]

    def claim(row):
        row["id"] = coordinate("claims", row["id"])
        row["question_slug"] = coordinate("question", row["question_slug"])
        row["premises"] = [coordinate("claims", identifier) for identifier in row["premises"]]
        for evidence in row["evidence"]:
            evidence["source_id"] = coordinate("sources", evidence["source_id"])

    def source(row):
        row["source_id"] = coordinate("sources", row["source_id"])

    def source_metadata(node):
        native = typed.typed_value(node)
        if "source_id" in native:
            native["source_id"] = coordinate("sources", native["source_id"])
        return typed.typed_node(native)

    def source_record_nodes(row, *, ancestry=False):
        fields = (("descriptor", "source_id"), ("grant", "source_id")) if ancestry else (
            ("manifest_record", "id"), ("revision_descriptor", "source_id"), ("grant", "source_id"))
        for key, coordinate_field in fields:
            native = typed.typed_value(row[key])
            native[coordinate_field] = coordinate("sources", native[coordinate_field])
            row[key] = typed.typed_node(native)
        if not ancestry:
            row["normalized_metadata"] = source_metadata(row["normalized_metadata"])

    def computation_result(result):
        for record in result["sources"]:
            source(record)
        for record in result["lineage"].values():
            if "source_id" in record:
                source(record)

    def question_record(row):
        row["slug"] = coordinate("question", row["slug"])
        row["original_id"] = coordinate("original", row["original_id"])

    value["claim_id"] = coordinate("claims", value["claim_id"])
    for row in value["claims"]:
        claim(row)
    for question in value["questions"]:
        question_record(question["manifest_question"])
        question["path"] = coordinate("paths", question["path"])
        mirrors = question["canonical_mirrors"]
        mirrors["source_ids"] = [coordinate("sources", identifier) for identifier in mirrors["source_ids"]]
        for grounding in mirrors["grounding"]:
            grounding["source_id"] = coordinate("sources", grounding["source_id"])
        for name in ("answer_page", "coverage_manifest"):
            mirrors[name] = coordinate("paths", mirrors[name])
    local = value["local_context"]
    for row in local["sources"]:
        source(row)
        row["metadata"] = source_metadata(row["metadata"])
    for row in local["mechanical"]:
        row["claim_id"] = coordinate("claims", row["claim_id"])
        row["question_slug"] = coordinate("question", row["question_slug"])
        row["original_id"] = coordinate("original", row["original_id"])
        row["premises"] = [coordinate("claims", identifier) for identifier in row["premises"]]
        for evidence in row["evidence"]:
            source(evidence)
    for row in local["mechanical_reasons"]:
        row["claim_id"] = coordinate("claims", row["claim_id"])
    counter = local["counterevidence"]
    for question in counter["questions"]:
        slug = question["question_slug"]
        native_slug = coordinate("question", slug) if inverse else slug
        question["question_slug"] = coordinate("question", slug)
        question["claim_ids"] = [coordinate("claims", identifier) for identifier in question["claim_ids"]]
        criteria = typed.typed_value(question["criteria"])
        for criterion in criteria:
            if inverse:
                joined = coordinate("facets", criterion["facet_id"])
                prefix = native_slug + ":"
                if not joined.startswith(prefix):
                    raise ExecutionError("review_context_alias_missing")
                criterion["facet_id"] = joined[len(prefix):]
            else:
                criterion["facet_id"] = coordinate("facets", native_slug + ":" + criterion["facet_id"])
        question["criteria"] = typed.typed_node(criteria)
        for alternative in question["alternative_sources"]:
            source(alternative)
    for row in counter["slices"]:
        source(row)
    scope = local["completeness"]["scope_qualifiers"]
    for key, name in (("claim_ids", "claims"), ("question_slugs", "question"),
            ("source_ids", "sources"), ("counter_question_slugs", "question")):
        scope[key] = [coordinate(name, identifier) for identifier in scope[key]]
    # Global envelopes have known inventory/source coordinates. Their opaque
    # typed native values, free prose and content commitments remain exact.
    global_material = value["global_material"]["material"]
    for row in global_material["global_dependencies"]:
        kind = row["kind"]
        if kind == "claims_inventory":
            row["value"]["ids"] = [coordinate("claims", identifier) for identifier in row["value"]["ids"]]
        elif kind == "questions_inventory":
            row["value"]["ids"] = [coordinate("question", identifier) for identifier in row["value"]["ids"]]
        elif kind == "source_inventory":
            for record in row["value"]["sources"]:
                source(record)
        elif kind == "source_record":
            row["coordinate"] = coordinate("sources", row["coordinate"])
            source(row["value"])
            source_record_nodes(row["value"])
            for record in row["value"]["ancestry"]:
                source(record)
                source_record_nodes(record, ancestry=True)
        elif kind == "computation":
            result = typed.typed_value(row["value"]["result"])
            computation_result(result)
            row["value"]["result"] = typed.typed_node(result)
    for row in global_material["worker_semantics"]["completeness"]["scope_qualifiers"]["global_dependency_coordinates"]:
        if row["kind"] == "source_record":
            row["coordinate"] = coordinate("sources", row["coordinate"])
    worker = global_material["worker_semantics"]
    if worker.get("computation") is not None:
        computation = worker["computation"]
        result = typed.typed_value(computation["result"])
        computation_result(result)
        computation["result"] = typed.typed_node(result)
        for row in computation["sources"]:
            source(row)
            row["metadata"] = source_metadata(row["metadata"])
    return value


def dependency_material(context):
    """Recover exact native semantic values from authenticated transport aliases."""
    packet, binding = context.document(), context.binding()
    if packet["schema_version"] != REVIEW_CONTEXT_V4:
        raise ExecutionError("review_context_version_unsupported")
    material = _translate_dependency_material(decode_dependency_material(packet["dependency_material"]), binding["aliases"], inverse=True)
    return _checked_dependency_material(material)


def _validate_dependency_context(packet, binding, packet_bytes, binding_bytes):
    if (set(packet) != _PACKET_FIELDS | {"questions", "dependency_material"}
            or set(binding) != _BINDING_FIELDS | {"dependency_material_sha256", "semantic_material_id", "review_producer_id"}
            or binding["schema_version"] != CONTEXT_BINDING_V4 or _json(packet) != packet_bytes or _json(binding) != binding_bytes
            or len(packet_bytes) > min(MAX_CONTEXT_BYTES, binding["max_bytes"])):
        raise ExecutionError("review_context_invalid")
    aliases = binding["aliases"]
    if (type(aliases) is not dict or set(aliases) != {"claims", "sources", "question", "original", "facets", "paths"}
            or any(type(mapping) is not dict or any(type(key) is not str or type(value) is not str
                for key, value in mapping.items()) for mapping in aliases.values())):
        raise ExecutionError("review_context_alias_invalid")
    material = _translate_dependency_material(decode_dependency_material(packet["dependency_material"]), aliases, inverse=True)
    _checked_dependency_material(material)
    from .pack_discovery import owner
    preimage = {key: value for key, value in material.items() if key != "dependency_basis_id"}
    semantic_id = owner("_evidence_revision").content_id("research-dependency-review-material/v1", preimage)
    if (binding["semantic_material_id"] != semantic_id or binding["review_producer_id"] != material["producer_id"]
            or binding["dependency_material_sha256"] != _native_digest(material)
            or binding["basis_id"] != material["dependency_basis_id"] or packet["basis_id"] != binding["basis_id"]
            or binding["packet_sha256"] != "sha256:" + _digest(packet)
            or binding["rubric_sha256"] != "sha256:" + _digest(packet["rubric"])
            or any(binding[key] != packet[key] for key in ("context_id", "task_role"))
            or binding["order_seed_sha256"] != "sha256:" + hashlib.sha256(bytes.fromhex(binding["order_seed"])).hexdigest()):
        raise ExecutionError("review_context_binding_mismatch")
    _projection(packet)
    legacy_packet = {key: value for key, value in packet.items() if key != "dependency_material"}
    legacy_packet["schema_version"] = REVIEW_CONTEXT_V3
    legacy_packet["questions"] = [{**row, "metadata": None} for row in packet["questions"]]
    legacy_binding = {key: value for key, value in binding.items()
        if key not in {"dependency_material_sha256", "semantic_material_id", "review_producer_id"}}
    legacy_binding.update(schema_version=CONTEXT_BINDING_V3, packet_sha256="sha256:" + _digest(legacy_packet),
        complete_material_sha256=binding["dependency_material_sha256"])
    ReviewContext(_json(legacy_packet), _json(legacy_binding))
    expected = [_claim(row, binding["aliases"]["claims"], binding["aliases"]["sources"], binding["aliases"]["question"])
        for row in material["claims"]]
    for projected, native in zip(expected, material["claims"], strict=True):
        projected["qualification"] = native["qualification"]
        if material["claim_schema_version"] == "evidence-strict-claims/v2":
            projected["calculations"] = json.loads(_json(native["calculations"]))
    if expected != [packet["claim"], *packet["premises"]] or not packet["manifest"]["context_complete"] or packet["manifest"]["truncated"]:
        raise ExecutionError("review_context_material_invalid")
    expected_questions = [{**row["manifest_question"], "slug": aliases["question"][row["manifest_question"]["slug"]],
        "original_id": aliases["original"][row["manifest_question"]["original_id"]],
        "metadata": row["semantic_frontmatter"], "body": row["body"]} for row in material["questions"]]
    typed = owner("_question_dependencies")
    worker = material["global_material"]["material"]["worker_semantics"]
    subject_slug = material["claims"][0]["question_slug"]
    question_ids = [subject_slug, *sorted({row["question_slug"] for row in material["claims"]} - {subject_slug})]
    subject_original = next(row["manifest_question"]["original_id"] for row in material["questions"]
        if row["manifest_question"]["slug"] == subject_slug)
    original_ids = [subject_original, *sorted({row["manifest_question"]["original_id"] for row in material["questions"]} - {subject_original})]
    source_ids = {evidence["source_id"] for row in material["claims"] for evidence in row["evidence"]}
    if worker["computation"] is not None:
        source_ids.update(row["source_id"] for row in worker["computation"]["sources"])
    expected_aliases = _dependency_aliases(material, {
        "claims": {row["id"]: "claim-" + str(index + 1) for index, row in enumerate(material["claims"])},
        "sources": {identifier: "source-" + str(index + 1) for index, identifier in enumerate(sorted(source_ids))},
        "question": {identifier: "question-" + str(index + 1) for index, identifier in enumerate(question_ids)},
        "original": {identifier: "original-" + str(index + 1) for index, identifier in enumerate(original_ids)}})
    if aliases != expected_aliases:
        raise ExecutionError("review_context_alias_invalid")
    if packet["questions"] != expected_questions or packet["rubric"] != typed.typed_value(worker["rubric"]):
        raise ExecutionError("review_context_material_invalid")
    actual_sources = {row["source_id"]: row for row in material["local_context"]["sources"]}
    computation = worker["computation"]
    if computation is not None:
        for row in computation["sources"]:
            previous = actual_sources.setdefault(row["source_id"], row)
            if previous != row:
                raise ExecutionError("review_context_material_invalid")
        from .calculation_context import project
        if packet["computation"] != project(typed.typed_value(computation["result"]), aliases["sources"]):
            raise ExecutionError("review_context_material_invalid")
    elif packet["computation"] is not None:
        raise ExecutionError("review_context_material_invalid")
    for row in packet["sources"]:
        identifier = next((identifier for identifier, alias in aliases["sources"].items() if alias == row["source_id"]), None)
        source = actual_sources.get(identifier)
        if source is None:
            raise ExecutionError("review_context_material_invalid")
        metadata = typed.typed_value(source["metadata"]) if source["metadata"].get("t") is not None else source["metadata"]
        expected_source = {**source, "source_id": row["source_id"], "metadata": _metadata(metadata)}
        if row != expected_source:
            raise ExecutionError("review_context_material_invalid")


def _create_dependency_context(*, context_id, task_role, basis_id, owner_snapshot, rubric, sources, order_seed, max_bytes, material):
    _checked_dependency_material(material)
    if basis_id != material["dependency_basis_id"] or material["claim_id"] != owner_snapshot["claim"]["id"]:
        raise ExecutionError("review_context_material_invalid")
    if any(row != next((native for native in material["claims"] if native["id"] == row["id"]), None)
            for row in [owner_snapshot["claim"], *owner_snapshot["premises"]]):
        raise ExecutionError("review_context_material_invalid")
    by_claim = {row["id"]: row for row in material["claims"]}
    legacy_snapshot = {key: owner_snapshot[key] for key in ("policy", "claim", "question", "source_revisions", "computation")}
    legacy_snapshot["premises"] = [by_claim[identifier] for identifier in owner_snapshot["claim"]["premises"]]
    complete = {"schema_version": COMPLETE_REVIEW_MATERIAL, "basis_id": basis_id,
        "native_snapshot_sha256": "sha256:" + _digest(legacy_snapshot), "claim_id": material["claim_id"],
        "claims": material["claims"], "questions": [{**row["manifest_question"],
            "metadata": None, "body": row["body"]} for row in material["questions"]]}
    legacy = create_review_context(context_id=context_id, task_role=task_role, basis_id=basis_id, owner_snapshot=legacy_snapshot,
        rubric=rubric, sources=sources, order_seed=order_seed, max_bytes=max_bytes, complete_material=complete)
    packet, binding = legacy.document(), legacy.binding()
    if not packet["manifest"]["context_complete"] or packet["manifest"]["truncated"]:
        raise ExecutionError("review_context_material_incomplete")
    aliases = _dependency_aliases(material, binding["aliases"])
    for row, question in zip(packet["questions"], material["questions"], strict=True):
        row["metadata"] = question["semantic_frontmatter"]
    packet.update(schema_version=REVIEW_CONTEXT_V4, dependency_material=_pack_native(_translate_dependency_material(material, aliases), "blinded_material"))
    from .pack_discovery import owner
    binding.pop("complete_material_sha256")
    binding.update(schema_version=CONTEXT_BINDING_V4, aliases=aliases, packet_sha256="sha256:" + _digest(packet),
        owner_snapshot_sha256=_native_digest(owner_snapshot),
        dependency_material_sha256=_native_digest(material), review_producer_id=material["producer_id"],
        semantic_material_id=owner("_evidence_revision").content_id("research-dependency-review-material/v1",
            {key: value for key, value in material.items() if key != "dependency_basis_id"}))
    return ReviewContext(_json(packet), _json(binding))
