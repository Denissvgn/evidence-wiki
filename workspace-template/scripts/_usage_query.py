#!/usr/bin/env python3
"""In-memory retrieval from currently permitted, exact normalized revisions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _evidence_authority import EvidenceInvalid, name
from _evidence_usage import MAX_NODES, current_view
from _record_artifacts import artifact_path, json_document
from _script_errors import ScriptRefusal
from _snapshot_qualifications import qualify_source
from _snapshot_verifier import SnapshotInvalid
from _usage_gate import (
    normalized_relative,
    read_workspace_file,
    refusal,
    requires_authority,
    resolve_revision,
    source_decision,
)


def read_manifest_bytes(root: Path, config: dict[str, Any]) -> bytes | None:
    """Read optional local restrictions without treating unreadable manifests as absent."""
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    relative = artifact_path(sources.get("manifest_path", "sources/manifest.jsonl"))
    try:
        (root / relative).lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise EvidenceInvalid("usage_manifest_unreadable") from exc
    return read_workspace_file(root, relative)


def manifest_records(data: bytes | None) -> dict[str, dict[str, Any]]:
    """Keep every local source declaration, refusing malformed or ambiguous identities."""
    records = {}
    for line in (data or b"").splitlines():
        if not line.strip():
            continue
        if len(records) >= MAX_NODES:
            raise EvidenceInvalid("usage_manifest_bound_exceeded")
        record = json_document(line)
        source_id = name(record.get("id"))
        if source_id in records:
            raise EvidenceInvalid("usage_manifest_source_duplicate")
        records[source_id] = record
    return records


def query_authorized(root: Path, config: dict[str, Any], scope: str, query: str,
                     limit: int, engine: Any) -> dict[str, Any]:
    """Keep the revocation read lock through ranking; never consume a persistent cache."""
    engine = SimpleNamespace(**engine)
    try:
        with current_view(root, config) as view:
            manifest = read_manifest_bytes(root, config)
            records = manifest_records(manifest)
            documents, denied = [], 0
            if scope in {"all", "normalized"}:
                for source_id in sorted({record["source_id"] for record in view.state.revisions.values()}):
                    relative = normalized_relative(config, source_id)
                    try:
                        data = read_workspace_file(root, relative)
                        frontmatter, body = engine.split_frontmatter(data.decode("utf-8"))
                        declarations = [records.get(source_id, {}), frontmatter]
                        decision = source_decision(root, config, source_id, declarations, view=view, normalized=data)
                        if not decision["eligible"] or frontmatter.get("source_id") != source_id:
                            denied += 1
                            continue
                        if requires_authority(declarations):
                            _revision, source = resolve_revision(view, source_id, decision["source_revision"], data)
                            qualify_source(source, source["files"])
                    except (EvidenceInvalid, SnapshotInvalid, ScriptRefusal, UnicodeDecodeError):
                        denied += 1
                        continue
                    document = engine.Document(path=relative, scope="normalized",
                                               kind=engine.document_kind(frontmatter, "normalized"),
                                               title=engine.extract_title(frontmatter, body, Path(relative).stem),
                                               headings=engine.extract_headings(body), source_ids=[source_id], body=body)
                    documents.append(engine.prepare_document(document))
            results = engine.add_engine(engine.rank_documents(documents, query, limit), engine.LEXICAL_ENGINE)
            if read_manifest_bytes(root, config) != manifest:
                raise EvidenceInvalid("usage_workspace_changed")
            return {"query": query, "scope": scope, "engine": engine.LEXICAL_ENGINE,
                    "indexed_documents": len(documents), "result_count": len(results), "results": results,
                    "warnings": [{"code": "QUERY_AUTHORIZED_REVISIONS_ONLY",
                                  "message": "Searched currently authorized normalized revisions in memory.",
                                  "remediation": "Materialize approved normalized revisions to include them."}],
                    "unnormalized_source_count": 0, "unnormalized_source_ids": [],
                    "usage": {"checkpoint": view.state.checkpoint, "excluded_source_count": denied,
                              "cache_used": False, "wiki_included": False}}
    except EvidenceInvalid as exc:
        raise refusal(str(exc)) from exc
