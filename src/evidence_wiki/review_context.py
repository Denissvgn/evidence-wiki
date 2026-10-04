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

from .agent_execution import ExecutionError, _digest, _integer, _json, _ref

REVIEW_CONTEXT = "research-review-context/v1"
CONTEXT_BINDING = "research-review-context-binding/v1"
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


def _projection(packet: dict[str, Any]) -> None:
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
    if type(packet["premises"]) is not list or len(packet["premises"]) > 32 or packet["computation"] is not None:
        raise ExecutionError("review_context_inputs_invalid")
    for claim in [packet["claim"], *packet["premises"]]:
        if (type(claim) is not dict or set(claim) != {"id", "question_slug", "kind", "premises", "evidence", *_CLAIM_FIELDS}
                or type(claim["id"]) is not str or re.fullmatch(r"claim-[1-9][0-9]{0,3}", claim["id"]) is None
                or type(claim["question_slug"]) is not str or re.fullmatch(r"question-[1-9][0-9]?", claim["question_slug"]) is None
                or claim["kind"] not in {"assertion", "inference"}
                or type(claim["premises"]) is not list or len(claim["premises"]) > 32
                or any(type(item) is not str or re.fullmatch(r"claim-[1-9][0-9]{0,3}", item) is None for item in claim["premises"])
                or type(claim["evidence"]) is not list or len(claim["evidence"]) > 16):
            raise ExecutionError("review_context_claim_invalid")
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
        if (type(packet) is not dict or set(packet) != _PACKET_FIELDS
                or packet["schema_version"] != REVIEW_CONTEXT or _json(packet) != self._packet_bytes
                or type(binding) is not dict or set(binding) != _BINDING_FIELDS
                or binding["schema_version"] != CONTEXT_BINDING or _json(binding) != self._binding_bytes
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
        if references != set(required):
            raise ExecutionError("review_context_manifest_invalid")
        claims = [packet["claim"], *packet["premises"]]
        claim_ids = {claim["id"] for claim in claims}
        unresolved_premises = {identifier for claim in claims for identifier in claim["premises"]} - claim_ids
        other_questions = any(claim["question_slug"] != "question-1" for claim in claims)
        if (len(claim_ids) != len(claims) or packet["claim"]["question_slug"] != "question-1"
                or ("source_context_missing" in manifest["limitations"]) != bool(missing)
                or ("source_context_truncated" in manifest["limitations"]) != bool(omitted)
                or ("premise_context_missing" in manifest["limitations"]) != bool(unresolved_premises)
                or ("premise_question_context_missing" in manifest["limitations"]) != other_questions):
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
        if _hash(basis_id) != binding["basis_id"] or "sha256:" + _digest(owner_snapshot) != binding["owner_snapshot_sha256"]:
            raise ExecutionError("review_context_owner_changed")

    def check(self, owner_snapshot: dict[str, Any], rubric: dict[str, Any], sources: list[dict[str, Any]]) -> None:
        """Reconstruct the exact current projection against retained parent inputs."""
        binding = self.binding()
        current = create_review_context(context_id=binding["context_id"], task_role=binding["task_role"],
            basis_id=binding["basis_id"], owner_snapshot=owner_snapshot, rubric=rubric, sources=sources,
            order_seed=bytes.fromhex(binding["order_seed"]), max_bytes=binding["max_bytes"])
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
                          max_bytes: int = MAX_CONTEXT_BYTES) -> ReviewContext:
    """Project complete owner inputs or explicitly disclose missing decisive context.

    Source contents remain exact inert evidence. Only control-plane metadata and
    canonical subject names are masked; scientifically relevant text is retained.
    Computational definitions need a future qualified projection; their presence
    currently marks this packet incomplete instead of silently guessing support.
    """
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
        if len(source_ids) > 64 or len(premise_ids) > 1024:
            raise ExecutionError("review_context_bound_invalid")
        claim_aliases = {identifier: f"claim-{index + 1}" for index, identifier in enumerate(claim_ids + sorted(premise_ids - set(claim_ids)))}
        source_aliases = {identifier: f"source-{index + 1}" for index, identifier in enumerate(sorted(source_ids))}
        question_ids = [question["slug"], *sorted({claim["question_slug"] for claim in claims} - {question["slug"]})]
        question_aliases = {identifier: f"question-{index + 1}" for index, identifier in enumerate(question_ids)}
        projected_claims = [_claim(claim, claim_aliases, source_aliases, question_aliases) for claim in claims]
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
        if len(question_ids) > 1:
            limitations.append("premise_question_context_missing")
        if owner_snapshot.get("computation") is not None or any(claim.get("calculations") for claim in claims):
            limitations.append("computation_context_unavailable")
        packet = {"schema_version": REVIEW_CONTEXT, "context_id": context_id, "task_role": task_role,
            "basis_id": basis_id, "rubric": projected_rubric, "claim": projected_claims[0],
            "question": {"slug": "question-1", "original_id": "original-1", "question": _text(question["question"])},
            "premises": projected_claims[1:], "sources": projected_sources, "computation": None,
            "manifest": {"required_source_ids": sorted(source_aliases.values()),
                "included_source_ids": [row["source_id"] for row in projected_sources], "missing_source_ids": missing,
                "omitted_source_ids": [], "context_complete": not limitations, "truncated": False,
                "limitations": limitations, "sections": []}}
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
        binding = {"schema_version": CONTEXT_BINDING, "context_id": context_id, "task_role": task_role,
            "basis_id": basis_id, "packet_sha256": "sha256:" + _digest(packet),
            "owner_snapshot_sha256": "sha256:" + _digest(owner_snapshot), "rubric_sha256": "sha256:" + _digest(projected_rubric),
            "source_input_sha256": "sha256:" + _digest(sources), "max_bytes": max_bytes,
            "aliases": {"claims": claim_aliases, "sources": source_aliases,
                "question": question_aliases, "original": {question["original_id"]: "original-1"}},
            "source_permutation": permutation, "order_seed_sha256": "sha256:" + hashlib.sha256(order_seed).hexdigest(),
            "order_seed": order_seed.hex()}
        return ReviewContext(_json(packet), _json(binding))
    except (KeyError, TypeError, IndexError, AttributeError):
        raise ExecutionError("review_context_inputs_invalid") from None
