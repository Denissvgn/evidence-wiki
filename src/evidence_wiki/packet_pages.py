"""Immutable blinded source pages; parent ownership and current rights are separate."""

from dataclasses import dataclass, field

from evidence_wiki._pack_io import bounded, canonical
from evidence_wiki.errors import UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.review_context import ReviewContext, _metadata

PACKET = "research-paged-review/v1"
PAGE = "research-review-page/v1"


def refuse(reason):
    raise UsageError("REVIEW_PAGE_REFUSED", "The review page is unavailable.", details={"reason": reason})


@dataclass(frozen=True)
class PacketPages:
    """Parent retains aliases and exact bodies; returned pages contain no signing data."""

    _header: bytes = field(repr=False)
    _pages: tuple[bytes, ...] = field(repr=False)
    _binding: bytes = field(repr=False)

    @property
    def packet_id(self):
        return digest(self.header())

    def header(self):
        import json
        return json.loads(self._header)

    def binding(self):
        import json
        return json.loads(self._binding)

    def page(self, page_id):
        import json
        if type(page_id) is not str:
            refuse("page_selector_invalid")
        for raw in self._pages:
            value = json.loads(raw)
            if value["page_id"] == page_id:
                return value
        refuse("page_unknown")

    def completeness(self, pages):
        if type(pages) is not list or len(pages) > 512:
            refuse("page_bound")
        expected = {row["page_id"]: row for row in self.header()["pages"]}
        seen = set()
        for page in pages:
            if type(page) is not dict or page.get("page_id") not in expected:
                refuse("foreign_page")
            pid = page["page_id"]
            if pid in seen or page != self.page(pid) or digest(page) != expected[pid]["sha256"]:
                refuse("page_changed_or_duplicate")
            seen.add(pid)
        return {"complete": seen == set(expected) and not self.header()["limitations"],
            "missing_page_ids": sorted(set(expected) - seen), "semantic_approval": "not_evaluated", "accepted": False}


def create(context, sources, *, page_chars=4096, sections=None):
    """Recover exact omitted source sections only from the complete parent inputs."""
    if type(context) is not ReviewContext or type(sources) is not list or len(sources) > 64:
        refuse("context_invalid")
    if type(page_chars) is not int or not 256 <= page_chars <= 16384:
        refuse("page_size_invalid")
    packet, binding = context.document(), context.binding()
    aliases = binding["aliases"]["sources"]
    by_source = {row["source_id"]: row for row in sources}
    if len(by_source) != len(sources):
        refuse("source_duplicate")
    projected = {row["source_id"]: row for row in packet["sources"]}
    section_map = {row["section_id"]: row for row in packet["manifest"]["sections"]}
    for identifier, alias in aliases.items():
        supplied = by_source.get(identifier)
        if supplied is None:
            continue
        revision = supplied.get("source_revision")
        if revision is None and alias in projected:
            revision = projected[alias]["source_revision"]
        row = {"source_id": alias, "source_revision": revision, "record_sha256": supplied["record_sha256"],
            "metadata": _metadata(supplied.get("metadata", {})), "content": supplied["content"]}
        section = section_map.get(alias)
        if section is None or digest(row) != section["sha256"] or len(canonical(row)) != section["bytes"]:
            refuse("source_section_changed")
        projected[alias] = row
    missing = sorted(set(aliases.values()) - set(projected))
    pages, descriptors = [], []
    order = [alias for alias in binding["source_permutation"] if alias in projected]
    for alias in order:
        row = projected[alias]
        content = row["content"]
        if type(content) is not str or len(content.encode()) > 65536:
            refuse("source_body_bound")
        for start in range(0, len(content), page_chars):
            page_id = alias + "." + str(start // page_chars + 1)
            page = {"schema_version": PAGE, "context_id": packet["context_id"], "context_sha256": context.packet_id,
                "page_id": page_id, "source_id": alias, "source_revision": row["source_revision"],
                "record_sha256": row["record_sha256"], "start": start, "end": min(start + page_chars, len(content)),
                "source_chars": len(content), "text": content[start:start + page_chars], "semantic_approval": "not_evaluated"}
            pages.append(canonical(page))
            descriptors.append({"page_id": page_id, "source_id": alias, "sha256": digest(page), "required": True})
    computation = packet["computation"]
    if computation is not None:
        serialized = canonical(computation).decode("utf-8")
        for start in range(0, len(serialized), page_chars):
            page_id = "computation." + str(start // page_chars + 1)
            page = {"schema_version": PAGE, "context_id": packet["context_id"], "context_sha256": context.packet_id,
                "page_id": page_id, "section_id": "computation", "section_sha256": digest(computation),
                "start": start, "end": min(start + page_chars, len(serialized)), "section_chars": len(serialized),
                "text": serialized[start:start + page_chars], "semantic_approval": "not_evaluated"}
            pages.append(canonical(page))
            descriptors.append({"page_id": page_id, "section_id": "computation", "sha256": digest(page), "required": True})
    if sections is not None:
        from .review_packet_contracts import validate
        sections = validate("research-review-sections/v1", sections)
        if sections["context_sha256"] != context.packet_id:
            refuse("decisive_sections_invalid")
        subjects = {row["id"] for row in [packet["claim"], *packet["premises"]]}
        mechanical = [row["claim_id"] for row in sections["mechanical"]]
        if set(mechanical) != subjects or len(mechanical) != len(subjects):
            refuse("mechanical_subject_context_missing")
        bounded(sections)
        serialized = canonical(sections).decode("utf-8")
        if len(serialized.encode()) > 196608:
            refuse("decisive_sections_bound")
        for start in range(0, len(serialized), page_chars):
            page_id = "mechanical-counter." + str(start // page_chars + 1)
            page = {"schema_version": PAGE, "context_id": packet["context_id"], "context_sha256": context.packet_id,
                "page_id": page_id, "section_id": "mechanical-counter", "section_sha256": digest(sections),
                "start": start, "end": min(start + page_chars, len(serialized)), "section_chars": len(serialized),
                "text": serialized[start:start + page_chars], "semantic_approval": "not_evaluated"}
            pages.append(canonical(page))
            descriptors.append({"page_id": page_id, "section_id": "mechanical-counter", "sha256": digest(page), "required": True})
    limitations = [value for value in packet["manifest"]["limitations"] if value not in {"source_context_missing", "source_context_truncated"}]
    if sections is None:
        limitations.append("mechanical_counter_context_unavailable")
    elif sections["counter_context"]["status"] != "current_native_instruction":
        limitations.append("counter_context_unavailable")
    if missing:
        limitations.append("source_context_missing")
    if len(pages) > 512 or sum(len(page) for page in pages) > 4194304:
        refuse("packet_bound")
    header = {"schema_version": PACKET, "context_id": packet["context_id"], "context_sha256": context.packet_id,
        "task_role": packet["task_role"], "basis_id": packet["basis_id"], "rubric": packet["rubric"],
        "claim": packet["claim"], "question": packet["question"], "premises": packet["premises"],
        "mechanical_counter_context": {"status": "retained" if sections is not None else "not_provided",
            "section_sha256": digest(sections) if sections is not None else None,
            "counter_search_completeness": "not_established"},
        "sources": [{key: projected[alias][key] for key in ("source_id", "source_revision", "record_sha256", "metadata")}
            for alias in order],
        "computation": {"required": computation is not None, "section_sha256": digest(computation) if computation is not None else None}, "pages": descriptors, "missing_source_ids": missing,
        "limitations": limitations, "complete_evidence_delivered": False, "semantic_approval": "not_evaluated", "accepted": False}
    bounded(header)
    return PacketPages(canonical(header), tuple(pages), canonical({"context_binding": binding,
        "header_sha256": digest(header), "source_input_sha256": digest(sources)}))
