"""Selected immutable source observations over native lexical ranking."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import time
from pathlib import Path

from evidence_wiki._pack_io import bounded, canonical, json_document
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.host_context import _require_mutation_workspace_gate, current_context
from evidence_wiki.pack_discovery import owner
from evidence_wiki.source_inputs import SourceView
from evidence_wiki.source_readiness import inspect_sources

from .retrieval_excerpts import excerpt

SCHEMA = "evidence-retrieval-view/v1"


def schemas():
    """Closed observations retain native permission facts without authority claims."""
    from evidence_wiki.source_contracts import array, obj, string
    number = {"type": "integer", "minimum": 0}
    boolean = {"type": "boolean"}
    digest = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$"}
    nullable_digest = {"anyOf": [digest, {"type": "null"}]}
    facts = {"type": "object", "maxProperties": 128, "additionalProperties": True}
    location = obj(text={"type": "string", "maxLength": 4096}, start_char=number, end_char=number,
        start_line={"type": "integer", "minimum": 1}, end_line={"type": "integer", "minimum": 1},
        prefix_omitted=boolean, suffix_omitted=boolean, excerpt_sha256=digest,
        location_basis={"const": "exact_normalized_body"}, original_location={"const": "not_inferred"},
        semantic_adequacy={"const": "not_evaluated"})
    row = obj(path=string(512), scope={"const": "normalized"}, kind=string(256),
        title={"type": "string", "maxLength": 1024}, source_ids=array(string(256), 1, 1),
        matched_headings=array({"type": "string", "maxLength": 512}, 16), score={"type": "number"},
        metadata_truncated=boolean, source_id=string(256), source_revision=digest,
        revision_basis={"const": "current_captured_local_material"}, normalized_sha256=digest,
        usage_revision=nullable_digest, permission=facts, excerpt=location)
    view = obj(schema_version={"const": SCHEMA}, query=string(4096), results=array(row, 32),
        status={"enum": ["found", "empty", "unknown", "truncated"]},
        coverage=obj(selected={"type": "integer", "minimum": 1, "maximum": 32},
            permitted_processed=number, permitted_unprocessed=number, complete=boolean,
            excluded_details={"const": "not_disclosed"}),
        engine=obj(requested={"enum": ["lexical", "auto", "fts"]}, used={"enum": ["lexical", "fts"]},
            index_status={"enum": ["not_requested", "current", "fallback", "eligible_memory_snapshot", "unavailable"]},
            fallback={"anyOf": [string(128), {"type": "null"}]}), observation=facts,
        workspace_binding=obj(root=string(4096), identity=obj(device=string(128), inode=string(128))),
        bounds=obj(sources={"const": 32}, selected_bytes={"const": 33_554_432}, per_file_bytes={"const": 16_777_216},
            output_bytes={"const": 1_048_576}, excerpt_chars={"type": "integer", "minimum": 128, "maximum": 4096}),
        index_writes={"const": False}, provider_calls={"const": False}, authorized={"const": False},
        evidence_accepted={"const": False}, semantic_adequacy={"const": "not_evaluated"})
    built = obj(schema_version={"const": "evidence-retrieval-index-result/v1"}, path=string(512),
        content_sha256=digest, changed=boolean, indexed_sources={"type": "integer", "minimum": 1, "maximum": 32},
        index_writes=boolean, authorized={"const": False}, evidence_accepted={"const": False})
    return {name: {"$schema": "https://json-schema.org/draft/2020-12/schema", **value}
        for name, value in ((SCHEMA, view), ("evidence-retrieval-index-result/v1", built))}


def refuse(field):
    raise UsageError("RETRIEVAL_INPUT_INVALID", "Retrieval input refused.", details={"field": field})


class _ScopedView(SourceView):
    def __init__(self, root):
        self.scope_error = None
        super().__init__(root)

    def read(self, relative, maximum=1_048_576):
        try:
            current_context().require_path(self.root / relative)
        except ConfigError as error:
            self.scope_error = error
            raise
        return super().read(relative, maximum)

    def recheck_scope(self):
        if self.scope_error is not None:
            raise self.scope_error
        for relative in self.files:
            current_context().require_path(self.root / relative)


def _selection(root, query, source_ids, engine, limit, excerpt_chars, index_path, *, grant="search", write=False):
    if (type(query) is not str or not query.strip() or len(query) > 4096
            or any(0xD800 <= ord(char) <= 0xDFFF for char in query)):
        refuse("query")
    if (type(source_ids) is not list or not 1 <= len(source_ids) <= 32
            or any(type(value) is not str or not 1 <= len(value) <= 256
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value) for value in source_ids)
            or len(set(source_ids)) != len(source_ids)):
        refuse("source_ids")
    if type(engine) is not str or engine not in {"auto", "lexical", "fts"}:
        refuse("engine")
    if type(limit) is not int or not 1 <= limit <= 32:
        refuse("limit")
    if type(excerpt_chars) is not int or not 128 <= excerpt_chars <= 4096:
        refuse("excerpt_chars")
    if index_path is not None:
        from evidence_wiki._pack_io import relative_path
        relative_path(index_path)
    context = current_context()
    if context is None:
        raise ConfigError("HOST_CONTEXT_REFUSED", "Retrieval requires current host selections.",
            details={"reason": "host_context_required"})
    context.require_grant("retrieval_operations." + grant)
    selected = context.require_path(Path(root), write=write).resolve(strict=True)
    return selected


def _documents(view, source_ids, *, usage_view=None):
    native, gate = owner("query_index"), owner("_usage_gate")
    contract, normalizer = owner("_normalized_contract"), owner("normalize_sources")
    records = view.manifest()
    documents, facts, unprocessed, unknown = [], {}, 0, False
    for source_id in source_ids:
        record = records.get(source_id)
        if record is None:
            unknown = True
            continue
        path = view.generated_path("normalized_dir", "sources/normalized") + "/" + normalizer.safe_source_id(source_id) + ".md"
        raw = view.read(path, 16_777_216)
        metadata, body = {}, ""
        if raw is not None:
            try:
                metadata, body, error = contract.split_record(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                metadata, error = {}, "normalized_invalid"
        else:
            error = "normalization_not_run"
        decision = gate.source_decision(view.root, view.config, source_id, [record, metadata or {}], normalized=raw, view=usage_view)
        if decision.get("eligible") is not True:
            unknown = True
            continue
        if error or metadata is None:
            unprocessed += 1
            continue
        inspection = inspect_sources(view, source_ids=[source_id])[0]
        view.recheck_scope()
        if inspection.get("usability") != "usable" or inspection.get("complete") is not True:
            unprocessed += 1
            continue
        document = native.prepare_document(native.Document(path=path, scope="normalized",
            kind=native.document_kind(metadata, "normalized"), title=native.extract_title(metadata, body, source_id),
            headings=native.extract_headings(body), source_ids=[source_id], body=body))
        documents.append(document)
        scope = owner("source_requests").source_provenance_scope(view.root, view.config, source_id)
        source_revision = "sha256:" + hashlib.sha256(canonical({"source_id": source_id,
            "normalized_sha256": inspection.get("normalized_sha256"), "originals": inspection.get("raw_sha256", {}),
            "scope": scope})).hexdigest()
        facts[path] = {"source_id": source_id, "source_revision": source_revision,
            "revision_basis": "current_captured_local_material", "normalized_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "usage_revision": decision.get("source_revision"), "permission": decision,
            "document": document}
    return documents, facts, unprocessed, unknown


def _insert(connection, documents):
    native = owner("query_index")
    native.create_fts_schema(connection)
    connection.executemany("INSERT INTO documents_fts (path,scope,kind,title,headings,source_ids,body,headings_json,source_ids_json,content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(document.path, document.scope, document.kind, document.title, " ".join(document.headings),
            " ".join(document.source_ids), document.body, json.dumps(document.headings, ensure_ascii=False),
            json.dumps(document.source_ids, ensure_ascii=False), native.document_content_hash(document)) for document in documents])


def _basis(facts):
    return {path: {key: row[key] for key in ("normalized_sha256", "source_revision")} for path, row in facts.items()}


def _fts_rank(documents, query):
    native = owner("query_index")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        _insert(connection, documents)
        expression = native.fts_match_query(query)
        if not expression:
            return []
        terms, phrase = set(native.tokenize(query)), " ".join(query.lower().split())
        scored = []
        for row in connection.execute(native.FTS_SELECT_SQL, [expression, max(1, len(documents))]):
            document = native.document_from_fts_row(row)
            score = -float(row["rank"]) * 1_000_000.0 + native.supplemental_boost(document, terms, phrase)
            if not math.isfinite(score):
                refuse("indexed_score")
            scored.append((score, native.SCOPE_TIE_BREAK.get(document.scope, 2), document.path, document))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [{"path": document.path, "scope": document.scope, "kind": document.kind,
            "title": document.title, "source_ids": document.source_ids,
            "matched_headings": native.matched_headings(document, terms), "score": round(score, 6)}
            for score, _scope, _path, document in scored]
    finally:
        connection.close()


def _index_status(view, path, documents, facts):
    if path is None:
        return False, "index_not_selected"
    raw = view.read(path, 16_777_216)
    if raw is None:
        return False, "index_missing"
    if not raw.startswith(b"SQLite format 3\x00"):
        return False, "index_unavailable"
    with tempfile.TemporaryDirectory(prefix="evidence-retrieval-index-") as temporary:
        snapshot = Path(temporary) / "index.sqlite"
        snapshot.write_bytes(raw)
        connection = None
        try:
            connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=0)
            connection.row_factory = sqlite3.Row
            if not hasattr(connection, "setlimit"):
                return False, "index_runtime_limits_unavailable"
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16_777_216)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 65_536)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 32)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA query_only=ON")
            started = time.monotonic()
            connection.set_progress_handler(lambda: int(time.monotonic() - started > 2), 1000)
            schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name LIMIT 17").fetchall()
            trusted = sqlite3.connect(":memory:")
            try:
                owner("query_index").create_fts_schema(trusted)
                expected = trusted.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchall()
            finally:
                trusted.close()
            if len(schema) > 16 or [tuple(row) for row in schema] != expected:
                return False, "index_schema_or_scope_unsupported"
            metadata_rows = connection.execute("SELECT key,value FROM metadata LIMIT 17").fetchall()
            if len(metadata_rows) > 16 or len({row["key"] for row in metadata_rows}) != len(metadata_rows):
                return False, "index_unavailable"
            metadata = {row["key"]: row["value"] for row in metadata_rows}
            if metadata.get("schema_version") != owner("query_index").INDEX_SCHEMA_VERSION or metadata.get("scope") != "normalized":
                return False, "index_schema_or_scope_unsupported"
            encoded = metadata.get("selected_source_basis", "null")
            if type(encoded) is not str or len(encoded.encode("utf-8")) > 65_536:
                return False, "index_unavailable"
            declared = json_document(encoded.encode("utf-8"))
            if type(declared) is not dict:
                return False, "index_revision_binding_missing"
            for document in documents:
                if declared.get(document.path) != _basis(facts)[document.path]:
                    return False, "index_stale_content"
                rows = connection.execute("SELECT * FROM documents_fts WHERE path = ? LIMIT 2", [document.path]).fetchall()
                if len(rows) != 1:
                    return False, "index_selected_document_missing"
                cached = owner("query_index").document_from_fts_row(rows[0])
                expected = owner("query_index").document_content_hash(document)
                if (rows[0]["content_hash"] != expected
                        or owner("query_index").document_content_hash(cached) != expected):
                    return False, "index_stale_content"
            return True, None
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, RecursionError, UsageError):
            return False, "index_unavailable"
        finally:
            if connection is not None:
                connection.close()


def search(root, *, query, source_ids, engine="auto", limit=10, excerpt_chars=1200, index_path=None):
    """Rank only currently permitted useful sources; observations accept no claims."""
    root = _selection(root, query, source_ids, engine, limit, excerpt_chars, index_path)
    view = _ScopedView(root)
    if view.workspace != "present":
        refuse("workspace")
    usage = owner("_evidence_usage")
    if usage.configured(view.config):
        with usage.current_view(root, view.config) as usage_view:
            result = _search(view, query, source_ids, engine, limit, excerpt_chars, index_path, usage_view=usage_view)
            usage_view.revalidate(root, view.config)
            return result
    return _search(view, query, source_ids, engine, limit, excerpt_chars, index_path)


def _search(view, query, source_ids, engine, limit, excerpt_chars, index_path, *, usage_view=None):
    documents, facts, unprocessed, unknown = _documents(view, source_ids, usage_view=usage_view)
    native = owner("query_index")
    used, fallback = "lexical", None
    index_status = "not_requested"
    if engine != "lexical" and documents:
        if not native.sqlite_fts5_available():
            fallback, index_status = "fts_unavailable", "unavailable"
        else:
            current, fallback = _index_status(view, index_path, documents, facts)
            index_status = "current" if current else "fallback"
            if current or engine == "fts" and index_path is None:
                used = "fts"
                fallback = None
                index_status = "eligible_memory_snapshot" if index_path is None else "current"
    ranked = (_fts_rank(documents, query) if used == "fts"
        else native.rank_documents(documents, query, max(1, len(documents))))
    if usage_view is not None:
        usage_view.revalidate(view.root, view.config)
    results = []
    for row in ranked[:limit]:
        fact = facts[row["path"]]
        result = {key: value for key, value in row.items() if key != "snippet"}
        headings = result.get("matched_headings", [])
        title = result.get("title", "")
        result["metadata_truncated"] = len(headings) > 16 or len(title) > 1024 or any(len(value) > 512 for value in headings)
        result["matched_headings"] = [value[:512] for value in headings[:16]]
        result["title"] = title[:1024]
        result.update({key: value for key, value in fact.items() if key != "document"})
        result["excerpt"] = excerpt(fact["document"].body, query, width=excerpt_chars)
        results.append(result)
    view.finish()
    observed = {"sha256": "sha256:" + hashlib.sha256(canonical(_basis(facts))).hexdigest(),
        "scope": "permitted_selected_material", "atomic_host_snapshot": False}
    result = {"schema_version": SCHEMA, "query": query, "results": results,
        "status": "truncated" if len(ranked) > limit else "found" if results else "unknown" if unknown or unprocessed else "empty",
        "coverage": {"selected": len(source_ids), "permitted_processed": len(documents),
            "permitted_unprocessed": unprocessed, "complete": not unknown and not unprocessed,
            "excluded_details": "not_disclosed"},
        "engine": {"requested": engine, "used": used, "index_status": index_status, "fallback": fallback},
        "observation": observed, "workspace_binding": {"root": str(view.root), "identity": view.root_identity},
        "bounds": {"sources": 32, "selected_bytes": 33_554_432, "per_file_bytes": 16_777_216,
            "output_bytes": 1_048_576, "excerpt_chars": excerpt_chars},
        "index_writes": False, "provider_calls": False,
        "authorized": False, "evidence_accepted": False, "semantic_adequacy": "not_evaluated"}
    bounded(result)
    if len(canonical(result)) > 1_048_576:
        refuse("output_bytes")
    from evidence_wiki.onboarding_contract import _matches
    for row in result["results"]:
        if type(row["score"]) not in {int, float} or not math.isfinite(row["score"]):
            refuse("score")
    _matches(result, schemas()[SCHEMA])
    return result


def build_index(root, *, source_ids, index_path="sources/indexes/evidence.sqlite", expected_sha256=None):
    """Publish an explicit selected local index under canonical mutation custody."""
    root = _selection(root, "index", source_ids, "lexical", 1, 128, index_path, grant="build_index", write=True)
    if current_context().mutation_control_directory is None:
        refuse("mutation_control_required")
    _require_mutation_workspace_gate(root)
    if not index_path.startswith("sources/indexes/") or not index_path.endswith(".sqlite"):
        refuse("index_path")
    context = current_context()
    context.require_path(root / index_path, write=True)
    if expected_sha256 is not None and (type(expected_sha256) is not str or len(expected_sha256) != 71
            or not expected_sha256.startswith("sha256:")
            or any(value not in "0123456789abcdef" for value in expected_sha256[7:])):
        refuse("expected_sha256")
    view = _ScopedView(root)
    if owner("_evidence_usage").configured(view.config):
        raise UsageError("RETRIEVAL_PROTECTED_INDEX_UNSUPPORTED", "Protected persistent indexing requires an owning approved export profile.")
    documents, facts, unprocessed, unknown = _documents(view, source_ids)
    if unprocessed or unknown or len(documents) != len(source_ids):
        refuse("selected_sources_not_current")
    native = owner("query_index")
    if not native.sqlite_fts5_available():
        refuse("fts_unavailable")
    connection = sqlite3.connect(":memory:")
    try:
        serializer = getattr(connection, "serialize", None)
        if not callable(serializer):
            raise UsageError("RETRIEVAL_INDEX_CAPABILITY_UNAVAILABLE", "Native index serialization is unavailable.",
                details={"field": "index_serialization_unavailable"})
        _insert(connection, documents)
        connection.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", [
            ("schema_version", native.INDEX_SCHEMA_VERSION), ("scope", "normalized"),
            ("document_count", str(len(documents))),
            ("selected_source_basis", json.dumps(_basis(facts), sort_keys=True, separators=(",", ":")))])
        connection.commit()
        raw = serializer()
    finally:
        connection.close()
    if len(raw) > 16_777_216:
        refuse("index_bytes")
    view.finish()
    lock = native.query_index_lock_path(root / index_path)
    context.require_path(lock, write=True)
    with owner("_workspace_locks").workspace_lock(lock, timeout_seconds=0, purpose="selected evidence index publication") as held:
        if not held.locked:
            refuse("native_index_lock_required")
        view.finish()
        changed = owner("_usage_materialization").publish_file(root, index_path, raw, expected_sha256,
            before_publish=lambda: view.finish())
    return {"schema_version": "evidence-retrieval-index-result/v1", "path": index_path,
        "content_sha256": "sha256:" + hashlib.sha256(raw).hexdigest(), "changed": changed,
        "indexed_sources": len(documents), "index_writes": changed,
        "authorized": False, "evidence_accepted": False}
