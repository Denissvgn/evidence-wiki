"""Current protected source custody around each immutable blinded page read."""

import hashlib
import re
import secrets
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from evidence_wiki import claim_diagnostics
from evidence_wiki._facades._base import _translated
from evidence_wiki._pack_io import canonical, json_document
from evidence_wiki.host_context import current_context
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.mutation_store import capture_revision
from evidence_wiki.pack_discovery import owner
from evidence_wiki.retrieval_operations import _documents, _ScopedView
from evidence_wiki.review_context import create_review_context

from .packet_pages import create, refuse
from .review_packet_sections import create as create_sections


@contextmanager
def snapshot(root, claim_id, *, source_ids=()):
    """Existing strict and retrieval owners select complete current permitted bytes."""
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
    selected = list(dict.fromkeys(item["source_id"] for claim in [prepared["snapshot"]["claim"], *prepared["snapshot"]["premises"]]
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
        if source_ids:
            from evidence_wiki.coverage_observation import observe
            counter_instruction = observe(root, question_slug=prepared["snapshot"]["claim"]["question_slug"], source_ids=selected)
        yield prepared, sources, diagnosed, counter_instruction, primary
        view.recheck_scope()
        view.finish()
        if live is not None:
            live.revalidate(root, view.config)
        if current_context() is not context or capture_revision(root, excluded=(".locks", "raw/.locks"))["revision_id"] != before:
            refuse("current_owner_changed")


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
    _workspace: object = field(default=None, repr=False)

    @contextmanager
    def current(self):
        if self._workspace is not None:
            self._workspace._check_open()
        context = current_context()
        if context is not self._context:
            refuse("host_selection_changed")
        context.require_grant("review_page_operations.page")
        with snapshot(self.root, self.claim_id, source_ids=self._source_ids) as (prepared, sources, diagnosed, counter, primary):
            self._review_context.check(prepared["snapshot"], prepared["rubric"], [row for row in sources if row["source_id"] in primary])
            if digest(sources) != self._source_input:
                refuse("source_input_changed")
            sections, _binding = create_sections(self._review_context, sources, diagnosed, counter_instruction=counter)
            if digest(sections) != self._section_input:
                refuse("mechanical_context_changed")
            yield prepared

    def header(self):
        with self.current():
            value = self._pages.header()
        from .review_packet_contracts import validate
        return validate("research-paged-review/v1", value)

    def page(self, page_id):
        with self.current():
            value = self._pages.page(page_id)
        from .review_packet_contracts import validate
        return validate("research-review-page/v1", value)

    def complete(self, delivered):
        with self.current():
            value = self._pages.completeness(delivered)
        return value


def prepare(root, *, claim_id, context_id, task_role="evaluator", page_chars=4096, source_ids=(), _order_seed=None):
    """Pin complete actual owner inputs; stored identity grants no source access."""
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
    with snapshot(root, claim_id, source_ids=source_ids) as (prepared, sources, diagnosed, counter, primary):
        review = create_review_context(context_id=context_id, task_role=task_role, basis_id=prepared["result"]["basis_id"],
            owner_snapshot=prepared["snapshot"], rubric=prepared["rubric"], sources=[row for row in sources if row["source_id"] in primary],
            order_seed=seed, max_bytes=262144)
        sections, _binding = create_sections(review, sources, diagnosed, counter_instruction=counter)
        pages = create(review, [row for row in sources if row["source_id"] in primary], page_chars=page_chars, sections=sections)
        if len(canonical(pages.header())) > 65536:
            refuse("header_bound")
        handle = ProtectedPages(root, claim_id, context, review, pages, digest(sources), digest(sections), tuple(source_ids))
    return handle
