"""In-memory retrieval from currently permitted, exact normalized revisions."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from _evidence_authority import EvidenceInvalid
from _evidence_usage import current_view
from _script_errors import ScriptRefusal
from _usage_gate import normalized_relative, read_workspace_file, refusal, source_decision


def query_authorized(root: Path, config: dict[str, Any], scope: str, query: str,
                     limit: int, engine: Any) -> dict[str, Any]:
    """Keep the revocation read lock through ranking; never consume a persistent cache."""
    engine = SimpleNamespace(**engine)
    try:
        with current_view(root, config) as view:
            documents, denied = [], 0
            if scope in {"all", "normalized"}:
                for source_id in sorted({record["source_id"] for record in view.state.revisions.values()}):
                    relative = normalized_relative(config, source_id)
                    try:
                        data = read_workspace_file(root, relative)
                        frontmatter, body = engine.split_frontmatter(data.decode("utf-8"))
                        decision = source_decision(root, config, source_id, [frontmatter], view=view, normalized=data)
                        if not decision["eligible"] or frontmatter.get("source_id") != source_id:
                            denied += 1
                            continue
                    except (EvidenceInvalid, ScriptRefusal, UnicodeDecodeError):
                        denied += 1
                        continue
                    document = engine.Document(path=relative, scope="normalized",
                                               kind=engine.document_kind(frontmatter, "normalized"),
                                               title=engine.extract_title(frontmatter, body, Path(relative).stem),
                                               headings=engine.extract_headings(body), source_ids=[source_id], body=body)
                    documents.append(engine.prepare_document(document))
            results = engine.add_engine(engine.rank_documents(documents, query, limit), engine.LEXICAL_ENGINE)
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
