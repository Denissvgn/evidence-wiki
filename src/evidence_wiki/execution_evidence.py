"""Current permitted evidence for native review and bounded worker proposals."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from .errors import ConfigError
from .mutation_contracts import canonical, digest
from .pack_discovery import owner
from .research_session_contracts import refuse
from .retrieval_excerpts import excerpt
from .retrieval_operations import _documents, _ScopedView


def sources(session, observed):
    """Reuse retrieval eligibility before exposing complete native source packets."""
    view = _ScopedView(session.root)
    records = view.manifest()
    frozen = session.policy.to_dict()
    native = frozen["scope"].get("source_scope_kind") == "native_extent"
    selected = list(records) if native else [key for key in records if key in frozen["scope"]["source_scope"]]
    scopes = []
    if native:
        declaration = (observed.requirements or {}).get("request", {}).get("payload", {}).get("authority", {})
        originals = declaration.get("source_scope", [])
        for value in frozen["scope"]["source_scope"]:
            path = owner("_owner_paths").checked_path(session.root, Path(value), field="source_extent")
            if (not path.is_relative_to(session.root)
                    or not any(Path(item).is_absolute() and path.is_relative_to(Path(item)) for item in originals)):
                refuse("SOURCE_SCOPE_UNSUPPORTED")
            if path.exists():
                session.context.require_path(path)
                scopes.append((path, path.is_dir()))
    extents = {}
    if native:
        for identifier in selected:
            record = records[identifier]
            paths = [path.relative_to(session.root).as_posix()
                for path in owner("source_inventory").raw_fingerprint_paths(session.root, record)]
            matches = []
            for scope in scopes:
                matched = False
                try:
                    owner("_draft_admission").original_source_paths(session.root, observed.capture, record, [scope], paths)
                    matched = True
                except ConfigError:
                    raise
                except (Exception, SystemExit):
                    matched = False
                if matched:
                    matches.append(str(scope[0]))
            if matches:
                extents[identifier] = sorted(matches)[0]
        selected = list(extents)
    if len(selected) > 32:
        refuse("SOURCE_LIMIT")
    packets, bindings = [], []
    usage = owner("_evidence_usage")
    context = usage.current_view(session.root, view.config) if usage.configured(view.config) else nullcontext(None)
    with context as live:
        _docs, facts, _unprocessed, _unknown = _documents(view, selected, usage_view=live)
        for fact in facts.values():
            identifier = fact["source_id"]
            extent = extents[identifier] if native else identifier
            raw = view.files[fact["document"].path]
            if len(raw) > 65_536:
                refuse("SOURCE_LIMIT")
            metadata, body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
            if error:
                continue
            binding = {"source_id": identifier, "extent_ref": extent, "closure_id": fact["source_revision"]}
            bindings.append(binding)
            packets.append({"source_id": identifier, "record_sha256": fact["normalized_sha256"],
                "metadata": metadata, "content": body, "source_binding": binding})
        view.recheck_scope()
        view.finish()
        if live is not None:
            live.revalidate(session.root, view.config)
    if len(canonical(packets)) > 196_608:
        refuse("SOURCE_LIMIT")
    return packets, bindings


def proposal_sources(packets, query, *, width=4096):
    """Bound generation context while preserving exact native record identity."""
    result = []
    for packet in packets:
        span = excerpt(packet["content"], query, width=width)
        row = {**packet, "content": span["text"], "excerpt": span,
            "excerpt_ref": digest({"source_id": packet["source_id"], "record_sha256": packet["record_sha256"],
                "source_binding": packet["source_binding"], "excerpt": span}),
            "context_complete": not span["prefix_omitted"] and not span["suffix_omitted"],
            "semantic_adequacy": "not_evaluated"}
        result.append(row)
    if len(canonical(result)) > 196_608:
        refuse("SOURCE_LIMIT")
    return result
