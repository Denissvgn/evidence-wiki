"""Current protected source custody around each immutable blinded page read."""

import asyncio
import copy
import hashlib
import re
import secrets
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from evidence_wiki import claim_diagnostics
from evidence_wiki._facades._base import _translated
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki._review_dependency_json import digest
from evidence_wiki.host_context import current_context
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.operation_control import OperationControl, checkpoint
from evidence_wiki.pack_discovery import owner
from evidence_wiki.retrieval_operations import _documents, _ScopedView
from evidence_wiki.review_context import create_review_context

from .packet_pages import create, refuse
from .review_material import current_review_material
from .review_packet_sections import create as create_sections

_MATERIAL = ContextVar("held_review_material", default=None)


def _material_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


@contextmanager
def _material_scope(root, prepared, material):
    lifetime = [True]
    observation = (root, prepared, digest(prepared), current_context(),
        threading.get_ident(), _material_task(), copy.deepcopy(material), lifetime)
    token = _MATERIAL.set(observation)
    try:
        yield
    finally:
        lifetime[0] = False
        _MATERIAL.reset(token)


def _held_material(root, prepared):
    observed = _MATERIAL.get()
    if (observed is not None and observed[7][0] and observed[0] == root and observed[1] is prepared
            and observed[2] == digest(prepared) and observed[3] is current_context()
            and observed[4] == threading.get_ident() and observed[5] is _material_task()):
        return copy.deepcopy(observed[6])
    return current_review_material(root, prepared)


@contextmanager
def snapshot(root, claim_id, *, source_ids=(), context_version="legacy", _closing_time=False, _closing_diagnostics=None):
    """Existing strict and retrieval owners select complete current permitted bytes."""
    if type(context_version) is not str or context_version not in {"legacy", "complete", "dependencies"}:
        refuse("context_version_invalid")
    context = current_context()
    if context is None or context.mutation_control_directory is None:
        refuse("current_host_control_required")
    root = context.require_path(Path(root)).resolve(strict=True)
    before = capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"]
    view = _ScopedView(root)
    try:
        prepared = owner("strict_evidence").prepare_review(root, claim_id)
    except Exception as error:
        translated = _translated(error)
        if translated is None:
            raise
        raise translated from None
    material = current_review_material(root, prepared) if context_version in {"complete", "dependencies"} else None
    if (prepared.get("review_schema") == "evidence-strict-review/v3") != (context_version == "dependencies"):
        refuse("context_version_invalid")
    claims = material["claims"] if material is not None else [prepared["snapshot"]["claim"], *prepared["snapshot"]["premises"]]
    selected = list(dict.fromkeys(item["source_id"] for claim in claims
        for item in claim["evidence"]))
    computation = prepared["snapshot"].get("computation")
    if computation is not None:
        selected = list(dict.fromkeys([*selected, *[row["source_id"] for row in computation["sources"]]]))
    primary = set(selected)
    if (type(source_ids) not in {list, tuple} or len(source_ids) > 32
            or any(type(identifier) is not str or not identifier or len(identifier) > 256 for identifier in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        refuse("source_selector_invalid")
    selected = list(dict.fromkeys([*selected, *source_ids]))
    if len(selected) > 32:
        refuse("source_bound")
    usage = owner("_evidence_usage")
    protected = usage.configured(view.config)
    with usage.current_view(root, view.config) if protected else nullcontext(None) as live:
        _docs, facts, _unprocessed, _unknown = _documents(view, selected, usage_view=live)
        sources = []
        for fact in facts.values():
            raw = view.files[fact["document"].path]
            metadata, body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
            if error:
                refuse("normalized_source_changed")
            sid = fact["source_id"]
            sources.append({"source_id": sid, "record_sha256": fact["normalized_sha256"], "metadata": metadata,
                "content": body, "source_revision": prepared["snapshot"]["source_revisions"].get(sid)})
        if set(selected) != {source["source_id"] for source in sources}:
            refuse("current_source_context_unavailable")
        policy = owner("_strict_evidence").resolve_policy(root, view.config)
        inventory = owner("_strict_contract").claims_document(json_document(view.read(policy["claims_path"])))
        originals = {}
        for question in inventory["questions"]:
            originals.setdefault(question["original_id"], []).append(question["slug"])
        from evidence_wiki.requirements_operations import read_effective
        effective = read_effective(root)
        declared = {row["id"]: row for row in effective["request"]["payload"]["questions"]}
        proposal = {"schema_version": "evidence-draft-proposal/v1", "inventory": inventory,
            "originals": [{"original_id": identifier, "text": declared[identifier]["text"], "question_slugs": slugs}
                for identifier, slugs in originals.items()]}
        diagnosed = claim_diagnostics.current(root, proposal)
        counter_instruction = None
        if material is not None:
            from evidence_wiki.coverage_observation import observe_questions
            counter_instruction = observe_questions(root, question_slugs=[
                question.get("manifest_question", question)["slug"] for question in material["questions"]], source_ids=selected)
        elif source_ids:
            from evidence_wiki.coverage_observation import observe
            counter_instruction = observe(root, question_slug=prepared["snapshot"]["claim"]["question_slug"], source_ids=selected)
        with _material_scope(root, prepared, material) if material is not None else nullcontext():
            yield prepared, sources, diagnosed, counter_instruction, primary
            if _closing_time:
                _recheck_time(root, view.config, prepared, inventory, diagnosed, policy, live)
            if _closing_diagnostics is not None and any(row["result"] != "verified" for claim in diagnosed["claims"] for row in claim["evidence"]):
                _closing_diagnostics(claim_diagnostics.current(root, proposal))
            view.recheck_scope()
            view.finish()
            if live is not None:
                live.revalidate(root, view.config)
            if current_context() is not context or capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"] != before:
                refuse("current_owner_changed")


def _recheck_time(root, config, prepared, inventory, diagnosed, policy, live):
    """Close native clock-dependent facts using the complete observed owner scope."""
    try:
        draft = owner("_claim_diagnostics").sibling("_draft_admission")
        authority = draft.sibling("_evidence_authority")
        closing = draft.clock(draft.current_time())
        draft.require(closing >= authority.timestamp(diagnosed["observed_at"]), "source_clock_regressed")
        by_claim = {row["claim_id"]: row for row in diagnosed["claims"]}
        for claim in inventory["claims"]:
            for index, evidence in enumerate(claim["evidence"]):
                if by_claim[claim["id"]]["evidence"][index]["result"] == "verified":
                    age = (closing - authority.timestamp(evidence["observed_at"])).total_seconds()
                    draft.require(0 <= age <= policy["max_source_age_seconds"], "source_age_invalid")
        if live is not None:
            live.revalidate(root, config)
            selected = prepared["snapshot"]["source_revisions"]
            ancestry = set(selected.values())
            for revision in selected.values():
                decision = live.check(revision, uses=["retrieval", "export"], purpose="qa-export", consumer="evidence-wiki")
                draft.require(decision["eligible"] and decision["complete"],
                    decision["reasons"][0] if decision["reasons"] else "assessment_use_incomplete")
                ancestry.update(decision["ancestors"])
            draft.require(len(ancestry) <= 128 and ancestry <= live.state.revisions.keys(), "assessment_source_ancestry_incomplete")
            temporal_revisions = [revision for revision in ancestry if live.state.revisions[revision]["descriptor"].get("temporal")]
            if temporal_revisions:
                owner("_assessment_engine").current_history(live, {"selected": selected, "ancestors": sorted(ancestry)})
                temporal = owner("_temporal_contract")
                strict = owner("_strict_evidence")
                for revision in ancestry:
                    times = temporal.source_times(live.state.revisions[revision])
                    strict.require(times["claims"]["expires_at"] is None or times["expires"] is not None, "strict_source_expiry_unknown")
                    strict.require(times["expires"] is None or live.now < times["expires"], "strict_source_expired")
                    if times["effective"] is not None:
                        start, end = times["effective"]
                        strict.require(start is not None and start <= live.now and (end is None or live.now < end), "strict_source_outside_effective_interval")
    except Exception as error:
        if isinstance(error, ValueError) and type(error).__name__ == "EvidenceInvalid":
            error = owner("_strict_evidence").refusal(str(error))
        translated = _translated(error)
        if translated is None:
            raise
        raise translated from None


@dataclass(frozen=True)
class ProtectedPages:
    """An immutable parent handle must be rechecked before any source page exits."""

    root: Path
    claim_id: str
    _context: object = field(repr=False)
    _review_context: object = field(repr=False)
    _pages: object = field(repr=False)
    _source_input: str = field(repr=False)
    _section_input: str = field(repr=False)
    _source_ids: tuple = field(repr=False)
    _context_version: str = field(default="legacy", repr=False)
    _workspace: object = field(default=None, repr=False)

    @contextmanager
    def current(self):
        if self._workspace is not None:
            self._workspace._check_open()
        context = current_context()
        if context is not self._context:
            refuse("host_selection_changed")
        context.require_grant("review_page_operations.page")
        with snapshot(self.root, self.claim_id, source_ids=self._source_ids, context_version=self._context_version) as (prepared, sources, diagnosed, counter, primary):
            self._check_snapshot(prepared, sources, diagnosed, counter, primary)
            yield prepared

    def _check_snapshot(self, prepared, sources, diagnosed, counter, primary):
        complete = ({"dependency_material" if self._context_version == "dependencies" else "complete_material": _held_material(self.root, prepared)}
            if self._context_version in {"complete", "dependencies"} else {})
        if complete:
            self._review_context.check_owner(owner_snapshot=prepared["snapshot"], basis_id=prepared.get("claim_basis_id", prepared["result"]["basis_id"]))
        self._review_context.check(prepared["snapshot"], prepared["rubric"], [row for row in sources if row["source_id"] in primary], **complete)
        if digest(sources) != self._source_input:
            refuse("source_input_changed")
        sections, _binding = create_sections(self._review_context, sources, diagnosed, counter_instruction=counter)
        if digest(sections) != self._section_input:
            refuse("mechanical_context_changed")

    def header(self):
        with self.current():
            value = self._pages.header()
        from .review_packet_contracts import validate
        return validate(value["schema_version"], value)

    def page(self, page_id):
        with self.current():
            value = self._pages.page(page_id)
        from .review_packet_contracts import validate
        return validate("research-review-page/v1", value)

    def complete(self, delivered):
        with self.current():
            value = self._pages.completeness(delivered)
        return value


@contextmanager
def _prepared_snapshot(root, *, claim_id, context_id, task_role="evaluator", page_chars=4096, source_ids=(), _order_seed=None,
        _closing_time=False, context_version="legacy"):
    """Keep preparation and private read validation inside current custody."""
    context = current_context()
    if context is None:
        refuse("current_host_control_required")
    context.require_grant("review_page_operations.prepare")
    if type(context_id) is not str or not 1 <= len(context_id) <= 256 or "\0" in context_id:
        refuse("context_reference_invalid")
    seed = secrets.token_bytes(32) if _order_seed is None else _order_seed
    if _order_seed is None:
        context_id = "context-" + hashlib.sha256(b"public-review-context/v1\0" + context_id.encode() + seed).hexdigest()[:32]
    elif re.fullmatch(r"context-[a-f0-9]{32}", context_id) is None:
        refuse("private_context_identity_invalid")
    root = context.require_path(Path(root)).resolve(strict=True)
    def check_closing(diagnostics):
        handle._check_snapshot(prepared, sources, diagnostics, counter, primary)
    current = snapshot(root, claim_id, source_ids=source_ids, context_version=context_version, _closing_time=True,
        _closing_diagnostics=check_closing) if _closing_time else snapshot(root, claim_id, source_ids=source_ids, context_version=context_version)
    with current as (prepared, sources, diagnosed, counter, primary):
        complete = {"dependency_material" if context_version == "dependencies" else "complete_material": _held_material(root, prepared)} if context_version in {"complete", "dependencies"} else {}
        review = create_review_context(context_id=context_id, task_role=task_role, basis_id=prepared.get("claim_basis_id", prepared["result"]["basis_id"]),
            owner_snapshot=prepared["snapshot"], rubric=prepared["rubric"], sources=[row for row in sources if row["source_id"] in primary],
            order_seed=seed, max_bytes=262144, **complete)
        sections, _binding = create_sections(review, sources, diagnosed, counter_instruction=counter)
        pages = create(review, [row for row in sources if row["source_id"] in primary], page_chars=page_chars, sections=sections)
        if len(canonical(pages.header())) > 65536:
            refuse("header_bound")
        handle = ProtectedPages(root, claim_id, context, review, pages, digest(sources), digest(sections), tuple(source_ids), context_version)
        yield handle, (prepared, sources, diagnosed, counter, primary)


def prepare(root, *, claim_id, context_id, task_role="evaluator", page_chars=4096, source_ids=(), _order_seed=None, context_version="legacy"):
    """Pin complete actual owner inputs; stored identity grants no source access."""
    with _prepared_snapshot(root, claim_id=claim_id, context_id=context_id, task_role=task_role,
            page_chars=page_chars, source_ids=source_ids, _order_seed=_order_seed, context_version=context_version) as (handle, _snapshot):
        pass
    return handle


def _collect_packet(root, *, claim_id, context_id, task_role="evaluator", page_chars=4096, source_ids=(),
        context_version="legacy", max_packet_bytes=196608, _order_seed=None, _control=None):
    """Collect one inert packet before closing its single current-owner scope."""
    if type(max_packet_bytes) is not int or not 4096 <= max_packet_bytes <= 196608:
        refuse("packet_bound")
    # Arbitrary host callbacks must never execute inside held source custody.
    if type(_control) is OperationControl and _control._native._cancel_requested is not None:
        refuse("packet_operation_control_invalid")
    checkpoint(_control, "before_packet_collection")
    version = "v3" if context_version == "dependencies" else "v2" if context_version == "complete" else "v1"
    with _prepared_snapshot(root, claim_id=claim_id, context_id=context_id, task_role=task_role,
            page_chars=page_chars, source_ids=source_ids, context_version=context_version, _order_seed=_order_seed,
            _closing_time=True) as (handle, current):
        context = current_context()
        if context is not handle._context:
            refuse("host_selection_changed")
        context.require_grant("review_page_operations.page")
        handle._check_snapshot(*current)
        from .review_packet_contracts import validate
        header = validate("research-paged-review/" + version, handle._pages.header())
        pages = []
        for row in header["pages"]:
            checkpoint(_control, "before_required_page")
            page = validate("research-review-page/v1", handle._pages.page(row["page_id"]))
            checkpoint(_control, "after_required_page")
            pages.append(page)
        complete = handle._pages.completeness(pages)
        packet = validate("research-review-packet/" + version, {"schema_version": "research-review-packet/" + version,
            "header": header, "pages": pages, "complete": complete})
        if not complete["complete"]:
            refuse("decisive_context_incomplete")
        if len(canonical(packet)) > max_packet_bytes:
            refuse("complete_packet_requires_supported_paging_executor")
    if current_context() is not context:
        refuse("host_selection_changed")
    context.require_grant("review_page_operations.prepare")
    context.require_grant("review_page_operations.page")
    checkpoint(_control, "after_packet_collection")
    return packet


@contextmanager
def _current_packet(root, *, packet, claim_id, context_id, task_role, page_chars, source_ids, _order_seed, context_version="legacy"):
    """Compare retained header and all pages without exporting a held approval."""
    with _prepared_snapshot(root, claim_id=claim_id, context_id=context_id, task_role=task_role,
            page_chars=page_chars, source_ids=source_ids, _order_seed=_order_seed, _closing_time=True, context_version=context_version) as (handle, current):
        context = current_context()
        if context is not handle._context:
            refuse("host_selection_changed")
        context.require_grant("review_page_operations.page")
        handle._check_snapshot(*current)
        from .review_packet_contracts import validate
        header = validate(handle._pages.header()["schema_version"], handle._pages.header())
        if (header != packet["header"] or handle._pages.completeness(packet["pages"]) != packet["complete"]
                or not packet["complete"]["complete"]):
            refuse("decisive_context_incomplete_or_stale")
        yield
        if current_context() is not context:
            refuse("host_selection_changed")
        context.require_grant("review_page_operations.prepare")
        context.require_grant("review_page_operations.page")
