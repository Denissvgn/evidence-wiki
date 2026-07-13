#!/usr/bin/env python3
"""Local lexical retrieval baseline for a research workspace.

``query_index.py`` is the dependency-light default retrieval layer described in
``docs/retrieval-upgrades.md``. Each run indexes Markdown files from the
configured ``wiki.root`` and ``sources.normalized_dir``, ranks keyword matches
in memory, and returns workspace-relative paths, headings, source IDs, and
snippets. It is deterministic and requires no persistent index or external
service, so agents can call it cheaply during the answering phase.

Maintained wiki pages remain preferred evidence over normalized source records,
which remain preferred over raw files. This tool only surfaces candidates; it
does not replace reading the cited pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to read research.yml and Markdown frontmatter") from exc


DEFAULT_WIKI_ROOT = "wiki"
DEFAULT_NORMALIZED_DIR = "sources/normalized"
DEFAULT_MANIFEST_PATH = "sources/manifest.jsonl"
DEFAULT_LIMIT = 10
MAX_QUERY_LIMIT = 100
# Manifest kinds that scripts/normalize_sources.py turns into normalized records.
# Used only to estimate how much discovered evidence is not yet searchable; keep
# in sync with normalize_sources.normalization_method. `table` records are
# normalizable only for delimited text files (CSV/TSV), checked per record.
NORMALIZABLE_KINDS = frozenset({"paper", "pdf", "web_link", "repo_link", "codebase_architecture", "html"})
TABLE_NORMALIZABLE_EXTENSIONS = (".csv", ".tsv")
MAX_REPORTED_UNNORMALIZED = 25
DEFAULT_INDEX_PATH = ".research-cache/query-index.sqlite3"
INDEX_SCHEMA_VERSION = "1"
PROVIDER_REQUEST_SCHEMA_VERSION = "1"
LEXICAL_ENGINE = "lexical"
HYBRID_ENGINE = "hybrid"
DEFAULT_RETRIEVAL_TIMEOUT_SECONDS = 30
DEFAULT_SEMANTIC_CACHE_DIR = ".research-cache/semantic-retrieval"
SEMANTIC_HYBRID_WEIGHT = 2.0
SCOPES = ("wiki", "normalized", "all")
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_locks import workspace_lock

# Field weights for lexical scoring. Titles, headings, and source IDs are
# stronger signals than body text.
WEIGHT_TITLE = 6
WEIGHT_HEADING = 3
WEIGHT_SOURCE_ID = 5
WEIGHT_BODY = 1
PHRASE_BOOST = 4
SOURCE_ID_EXACT_BOOST = 8
SCOPE_TIE_BREAK = {"wiki": 0, "normalized": 1}

TOKEN_RE = re.compile(r"[a-z0-9]+")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
H1_RE = re.compile(r"^#\s+(.*\S)\s*$")

FTS_SELECT_SQL = """
SELECT
    path,
    scope,
    kind,
    title,
    headings,
    source_ids,
    body,
    headings_json,
    source_ids_json,
    content_hash,
    bm25(documents_fts, 0.0, 0.0, 0.0, 6.0, 3.0, 5.0, 1.0, 0.0, 0.0, 0.0) AS rank
FROM documents_fts
WHERE documents_fts MATCH ?
ORDER BY
    rank ASC,
    CASE scope
        WHEN 'wiki' THEN 0
        WHEN 'normalized' THEN 1
        ELSE 2
    END ASC,
    path ASC
LIMIT ?
"""

FTS_SELECT_SCOPED_SQL = """
SELECT
    path,
    scope,
    kind,
    title,
    headings,
    source_ids,
    body,
    headings_json,
    source_ids_json,
    content_hash,
    bm25(documents_fts, 0.0, 0.0, 0.0, 6.0, 3.0, 5.0, 1.0, 0.0, 0.0, 0.0) AS rank
FROM documents_fts
WHERE documents_fts MATCH ?
AND scope = ?
ORDER BY
    rank ASC,
    CASE scope
        WHEN 'wiki' THEN 0
        WHEN 'normalized' THEN 1
        ELSE 2
    END ASC,
    path ASC
LIMIT ?
"""


@dataclass
class Document:
    path: str
    scope: str
    kind: str
    title: str
    headings: list[str]
    source_ids: list[str]
    body: str
    title_tokens: list[str] = field(default_factory=list)
    heading_tokens: list[str] = field(default_factory=list)
    source_id_tokens: list[str] = field(default_factory=list)
    body_tokens: list[str] = field(default_factory=list)
    body_lower: str = ""
    source_ids_lower: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RetrievalProvider:
    name: str
    command: list[str]
    timeout_seconds: float


@dataclass(frozen=True)
class SemanticRetrievalProvider:
    name: str
    transport: str
    command: list[str] | None
    endpoint: str | None
    timeout_seconds: float
    cache_dir: str


class RetrievalProviderError(Exception):
    """Provider configuration or response could not be trusted."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if raw_argv and raw_argv[0] == "build-index":
        return parse_build_index_args(raw_argv[1:])
    return parse_query_args(raw_argv)


def parse_query_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search maintained wiki pages and normalized source records by keyword."
    )
    parser.add_argument("query", nargs="+", help="Query terms to search for.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="all",
        help="Limit search to wiki pages, normalized records, or both. Defaults to all.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=(
            f"Maximum number of results to return. Defaults to {DEFAULT_LIMIT}; "
            f"values above {MAX_QUERY_LIMIT} are capped."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--index-path",
        default=None,
        help=(
            "Optional workspace-relative SQLite FTS index path. Defaults to "
            f"{DEFAULT_INDEX_PATH} under the project root when present."
        ),
    )
    args = parser.parse_args(argv)
    args.command = "query"
    return args


def effective_query_limit(limit: int) -> int:
    return min(max(0, limit), MAX_QUERY_LIMIT)


def parse_build_index_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="query_index.py build-index",
        description="Build a generated SQLite FTS index for the research workspace.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--scope",
        choices=SCOPES,
        default="all",
        help="Limit indexed documents to wiki pages, normalized records, or both. Defaults to all.",
    )
    parser.add_argument(
        "--index-path",
        default=None,
        help=(
            "Workspace-relative SQLite FTS index path. Defaults to "
            f"{DEFAULT_INDEX_PATH} under the project root."
        ),
    )
    args = parser.parse_args(argv)
    args.command = "build-index"
    return args


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    config = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def validate_workspace_relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"research.yml {label} must be a non-empty workspace-relative path")
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    parsed = urlparse(normalized)
    if "://" in normalized or parsed.scheme:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise SystemExit(f"research.yml {label} must be a workspace-relative path, not an absolute path: {value}")
    if ".." in path.parts:
        raise SystemExit(f"research.yml {label} must be a workspace-relative path without '..': {value}")
    return path.as_posix()


def validate_generated_sources_path(value: Any, label: str) -> str:
    relative = validate_workspace_relative_path(value, label).rstrip("/")
    if relative != "sources" and not relative.startswith("sources/"):
        raise SystemExit(f"research.yml {label} must be under the generated sources/ directory: {value}")
    return relative


def wiki_root(project_root: Path, config: dict[str, Any]) -> Path:
    wiki_config = config.get("wiki") if isinstance(config.get("wiki"), dict) else {}
    root = wiki_config.get("root") if isinstance(wiki_config.get("root"), str) else DEFAULT_WIKI_ROOT
    return project_root / validate_workspace_relative_path(root, "wiki.root")


def normalized_dir(project_root: Path, config: dict[str, Any]) -> Path:
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    normalized = (
        sources_config.get("normalized_dir")
        if isinstance(sources_config.get("normalized_dir"), str)
        else DEFAULT_NORMALIZED_DIR
    )
    return project_root / validate_generated_sources_path(normalized, "sources.normalized_dir")


def manifest_path(project_root: Path, config: dict[str, Any]) -> Path:
    sources_config = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    value = (
        sources_config.get("manifest_path")
        if isinstance(sources_config.get("manifest_path"), str)
        else DEFAULT_MANIFEST_PATH
    )
    return project_root / validate_workspace_relative_path(value, "sources.manifest_path")


def safe_source_id(source_id: str) -> str:
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.replace("__colon__", "--")
    value = value.replace("-.", ".").strip("-")
    return value or "source"


def is_normalizable_record(record: dict[str, Any]) -> bool:
    kind = record.get("kind")
    if kind in NORMALIZABLE_KINDS:
        return True
    if kind == "table":
        raw_paths = record.get("raw_paths")
        if isinstance(raw_paths, list):
            return any(
                isinstance(raw_path, str) and raw_path.lower().endswith(TABLE_NORMALIZABLE_EXTENSIONS)
                for raw_path in raw_paths
            )
    return False


def unnormalized_source_ids(project_root: Path, config: dict[str, Any]) -> list[str]:
    """Manifest sources that should be normalized but have no normalized record yet.

    These are real evidence that the query layer cannot search, so the answering
    agent gets a signal that the pipeline has unprocessed sources.
    """
    path = manifest_path(project_root, config)
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    normalized_root = normalized_dir(project_root, config)
    missing: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not is_normalizable_record(record):
            continue
        source_id = record.get("id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            continue
        if not (normalized_root / f"{safe_source_id(source_id)}.md").is_file():
            seen.add(source_id)
            missing.append(source_id)
    return sorted(missing)


def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # Frontmatter must open with a line that is exactly `---`.
    if not lines or lines[0].strip() != "---":
        return {}, text
    # Close on the first subsequent line that is exactly `---`, so a horizontal
    # rule (`----`) or a `---`-prefixed value inside the block does not truncate it.
    closing_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing_index is None:
        return {}, text
    block = "\n".join(lines[1:closing_index])
    body = "\n".join(lines[closing_index + 1 :])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(data, dict):
        return {}, body
    return data, body


def extract_source_ids(frontmatter: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    raw_list = frontmatter.get("source_ids")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str) and item.strip():
                source_ids.append(item.strip())
    single = frontmatter.get("source_id")
    if isinstance(single, str) and single.strip():
        source_ids.append(single.strip())
    # Preserve order while removing duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for source_id in source_ids:
        if source_id not in seen:
            seen.add(source_id)
            unique.append(source_id)
    return unique


def extract_reference_source_ids(frontmatter: dict[str, Any]) -> list[str]:
    reference_ids: list[str] = []
    raw_list = frontmatter.get("references_source_ids")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str) and item.strip():
                reference_ids.append(item.strip())
    seen: set[str] = set()
    unique: list[str] = []
    for source_id in reference_ids:
        if source_id not in seen:
            seen.add(source_id)
            unique.append(source_id)
    return unique


def extract_title(frontmatter: dict[str, Any], body: str, fallback: str) -> str:
    for line in body.splitlines():
        match = H1_RE.match(line)
        if match:
            return match.group(1).strip()
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    summary = frontmatter.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return fallback


def extract_headings(body: str) -> list[str]:
    headings: list[str] = []
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match:
            headings.append(match.group(1).strip())
    return headings


def document_kind(frontmatter: dict[str, Any], scope: str) -> str:
    if scope == "normalized":
        kind = frontmatter.get("source_kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
    kind = frontmatter.get("type")
    if isinstance(kind, str) and kind.strip():
        return kind.strip()
    return "unknown"


def build_document(project_root: Path, path: Path, scope: str) -> Document | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    frontmatter, body = split_frontmatter(text)
    rel_path = path.relative_to(project_root).as_posix()
    title = extract_title(frontmatter, body, path.stem)
    headings = extract_headings(body)
    source_ids = extract_source_ids(frontmatter)
    document = Document(
        path=rel_path,
        scope=scope,
        kind=document_kind(frontmatter, scope),
        title=title,
        headings=headings,
        source_ids=source_ids,
        body=body,
    )
    return prepare_document(document)


def prepare_document(document: Document) -> Document:
    document.title_tokens = tokenize(document.title)
    document.heading_tokens = tokenize(" ".join(document.headings))
    document.source_id_tokens = tokenize(" ".join(document.source_ids))
    document.body_tokens = tokenize(document.body)
    document.body_lower = document.body.lower()
    document.source_ids_lower = {source_id.lower() for source_id in document.source_ids}
    return document


def collect_documents(root: Path, project_root: Path, scope: str) -> list[Document]:
    documents: list[Document] = []
    if not root.is_dir():
        return documents
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        document = build_document(project_root, path, scope)
        if document is not None:
            documents.append(document)
    return documents


def scope_roots(project_root: Path, config: dict[str, Any], scope: str) -> list[tuple[str, Path]]:
    """Map a query scope to the (document scope, root directory) pairs it covers."""
    roots: list[tuple[str, Path]] = []
    if scope in ("wiki", "all"):
        roots.append(("wiki", wiki_root(project_root, config)))
    if scope in ("normalized", "all"):
        roots.append(("normalized", normalized_dir(project_root, config)))
    return roots


def corpus_roots_for_provider(project_root: Path, config: dict[str, Any], scope: str) -> list[dict[str, str]]:
    roots: list[dict[str, str]] = []
    for document_scope, root in scope_roots(project_root, config, scope):
        try:
            relative = root.relative_to(project_root).as_posix()
        except ValueError:
            relative = root.as_posix()
        roots.append({"scope": document_scope, "path": relative})
    return roots


def build_index(project_root: Path, config: dict[str, Any], scope: str) -> list[Document]:
    documents: list[Document] = []
    for document_scope, root in scope_roots(project_root, config, scope):
        documents.extend(collect_documents(root, project_root, document_scope))
    return documents


def citation_relation_graph(project_root: Path, config: dict[str, Any]) -> dict[str, list[str]]:
    graph: dict[str, set[str]] = {}
    root = normalized_dir(project_root, config)
    if not root.is_dir():
        return {}
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        frontmatter, _body = split_frontmatter(text)
        source_ids = extract_source_ids(frontmatter)
        reference_ids = extract_reference_source_ids(frontmatter)
        if not source_ids or not reference_ids:
            continue
        for source_id in source_ids:
            source_related = graph.setdefault(source_id, set())
            for reference_id in reference_ids:
                if reference_id == source_id:
                    continue
                source_related.add(reference_id)
                graph.setdefault(reference_id, set()).add(source_id)
    return {source_id: sorted(related_ids) for source_id, related_ids in graph.items()}


def evidence_path_graph(project_root: Path, config: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Index normalized and maintained paths by source id for bidirectional navigation."""

    graph: dict[str, dict[str, set[str]]] = {}
    for scope, root in scope_roots(project_root, config, "all"):
        if not root.is_dir():
            continue
        path_kind = "normalized_paths" if scope == "normalized" else "maintained_paths"
        for path in sorted(root.rglob("*.md"), key=lambda item: item.as_posix()):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
                relative = path.relative_to(project_root).as_posix()
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            frontmatter, _body = split_frontmatter(text)
            for source_id in extract_source_ids(frontmatter):
                entry = graph.setdefault(
                    source_id,
                    {"normalized_paths": set(), "maintained_paths": set()},
                )
                entry[path_kind].add(relative)
    return {
        source_id: {
            "normalized_paths": sorted(paths["normalized_paths"]),
            "maintained_paths": sorted(paths["maintained_paths"]),
        }
        for source_id, paths in graph.items()
    }


def enrich_related_source_ids(
    results: list[dict[str, Any]],
    relation_graph: dict[str, list[str]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        own_source_ids = {source_id for source_id in item.get("source_ids", []) if isinstance(source_id, str)}
        related: set[str] = set()
        for source_id in own_source_ids:
            related.update(relation_graph.get(source_id, []))
        item["related_source_ids"] = sorted(related - own_source_ids)
        enriched.append(item)
    return enriched


def enrich_evidence_links(
    results: list[dict[str, Any]],
    path_graph: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """Attach both normalized and maintained backlinks to every query result."""

    enriched: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        normalized_paths: set[str] = set()
        maintained_paths: set[str] = set()
        for source_id in item.get("source_ids", []):
            if not isinstance(source_id, str):
                continue
            links = path_graph.get(source_id, {})
            normalized_paths.update(links.get("normalized_paths", []))
            maintained_paths.update(links.get("maintained_paths", []))
        current_path = item.get("path")
        backlinks = sorted(
            path
            for path in normalized_paths | maintained_paths
            if path != current_path
        )
        item["evidence_links"] = {
            "normalized_paths": sorted(normalized_paths),
            "maintained_paths": sorted(maintained_paths),
            "backlinks": backlinks,
        }
        enriched.append(item)
    return enriched


def iter_scope_markdown_files(project_root: Path, config: dict[str, Any], scope: str) -> list[Path]:
    files: list[Path] = []
    for _document_scope, root in scope_roots(project_root, config, scope):
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if path.is_file():
                files.append(path)
    return files


def corpus_fingerprint(project_root: Path, config: dict[str, Any], scope: str) -> str:
    """Stat-only fingerprint of the indexed corpus for cheap staleness detection.

    Captures additions, deletions, and modifications via each file's path, size,
    and modification time without reading contents, so a fresh persistent index
    can be trusted without re-reading every document on each query.
    """
    entries: list[str] = []
    for path in sorted(iter_scope_markdown_files(project_root, config, scope), key=lambda item: item.as_posix()):
        try:
            stat = path.stat()
            relative = path.relative_to(project_root).as_posix()
        except (OSError, ValueError):
            continue
        entries.append(f"{relative}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def count_terms(tokens: list[str], terms: set[str]) -> int:
    return sum(1 for token in tokens if token in terms)


def best_snippet(document: Document, terms: set[str], width: int = 240) -> str:
    lowered = document.body_lower
    best_index = -1
    for term in terms:
        index = lowered.find(term)
        if index != -1 and (best_index == -1 or index < best_index):
            best_index = index
    source_text = " ".join(document.body.split())
    if not source_text:
        return ""
    if best_index == -1:
        return source_text[:width].strip()
    # Map the match position in the raw body to the collapsed text loosely by
    # re-finding the first term in the collapsed text.
    collapsed_lower = source_text.lower()
    pivot = -1
    for term in terms:
        index = collapsed_lower.find(term)
        if index != -1 and (pivot == -1 or index < pivot):
            pivot = index
    if pivot == -1:
        return source_text[:width].strip()
    start = max(0, pivot - width // 3)
    end = min(len(source_text), start + width)
    snippet = source_text[start:end].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(source_text) else ""
    return f"{prefix}{snippet}{suffix}"


def matched_headings(document: Document, terms: set[str]) -> list[str]:
    matches: list[str] = []
    for heading in document.headings:
        heading_terms = set(tokenize(heading))
        if heading_terms & terms:
            matches.append(heading)
    return matches


def score_document(document: Document, terms: set[str], phrase: str) -> int:
    score = 0
    score += WEIGHT_TITLE * count_terms(document.title_tokens, terms)
    score += WEIGHT_HEADING * count_terms(document.heading_tokens, terms)
    score += WEIGHT_SOURCE_ID * count_terms(document.source_id_tokens, terms)
    score += WEIGHT_BODY * count_terms(document.body_tokens, terms)
    if phrase and len(terms) > 1 and phrase in document.body_lower:
        score += PHRASE_BOOST
    if phrase in document.source_ids_lower:
        score += SOURCE_ID_EXACT_BOOST
    return score


def supplemental_boost(document: Document, terms: set[str], phrase: str) -> int:
    boost = 0
    if phrase and len(terms) > 1 and phrase in document.body_lower:
        boost += PHRASE_BOOST
    if phrase in document.source_ids_lower:
        boost += SOURCE_ID_EXACT_BOOST
    return boost


def rank_documents(documents: list[Document], query: str, limit: int) -> list[dict[str, Any]]:
    limit = effective_query_limit(limit)
    terms = set(tokenize(query))
    phrase = " ".join(query.lower().split())
    scored: list[tuple[int, int, str, Document]] = []
    for document in documents:
        score = score_document(document, terms, phrase)
        if score <= 0:
            continue
        scope_rank = SCOPE_TIE_BREAK.get(document.scope, 2)
        scored.append((score, scope_rank, document.path, document))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    results: list[dict[str, Any]] = []
    for score, _scope_rank, _path, document in scored[:limit]:
        results.append(
            {
                "path": document.path,
                "scope": document.scope,
                "kind": document.kind,
                "title": document.title,
                "source_ids": document.source_ids,
                "matched_headings": matched_headings(document, terms),
                "snippet": best_snippet(document, terms),
                "score": score,
            }
        )
    return results


def command_args(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            args = shlex.split(value)
        except ValueError as exc:
            raise RetrievalProviderError(f"command could not be parsed: {exc}") from exc
        if args:
            return args
    elif isinstance(value, list):
        args = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if len(args) == len(value) and args:
            return args
    raise RetrievalProviderError("command must be a non-empty string or list of strings")


def retrieval_provider(config: dict[str, Any]) -> RetrievalProvider | None:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    retrieval = integrations.get("retrieval") if isinstance(integrations.get("retrieval"), dict) else {}
    provider = retrieval.get("provider", LEXICAL_ENGINE)
    if not isinstance(provider, str) or provider.strip() in ("", "none", LEXICAL_ENGINE):
        return None
    name = provider.strip()
    command_value = retrieval.get("command")
    if command_value is None:
        warn_provider_failure(name, "command is not configured")
        return None
    try:
        command = command_args(command_value)
    except RetrievalProviderError as exc:
        warn_provider_failure(name, str(exc))
        return None
    timeout = retrieval.get("timeout_seconds", DEFAULT_RETRIEVAL_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS
    return RetrievalProvider(name=name, command=command, timeout_seconds=float(timeout))


def semantic_retrieval_provider(config: dict[str, Any]) -> SemanticRetrievalProvider | None:
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    retrieval = integrations.get("retrieval") if isinstance(integrations.get("retrieval"), dict) else {}
    semantic = retrieval.get("semantic") if isinstance(retrieval.get("semantic"), dict) else {}
    if semantic.get("enabled") is not True:
        return None
    provider = semantic.get("provider", "semantic")
    if not isinstance(provider, str) or not provider.strip():
        warn_provider_failure("semantic", "provider name must be non-empty")
        return None
    transport = semantic.get("transport", "command")
    if transport not in {"command", "http"}:
        warn_provider_failure(provider.strip(), "semantic transport must be command or http")
        return None
    timeout = semantic.get("timeout_seconds", DEFAULT_RETRIEVAL_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS
    cache_dir = semantic.get("cache_dir", DEFAULT_SEMANTIC_CACHE_DIR)
    try:
        cache_dir_text = validate_workspace_relative_path(cache_dir, "integrations.retrieval.semantic.cache_dir")
    except SystemExit as exc:
        warn_provider_failure(provider.strip(), str(exc))
        return None
    if cache_dir_text != ".research-cache" and not cache_dir_text.startswith(".research-cache/"):
        warn_provider_failure(provider.strip(), "semantic cache_dir must be under .research-cache/")
        return None
    command = None
    endpoint = None
    if transport == "command":
        try:
            command = command_args(semantic.get("command"))
        except RetrievalProviderError as exc:
            warn_provider_failure(provider.strip(), str(exc))
            return None
    else:
        endpoint_value = semantic.get("endpoint")
        if not isinstance(endpoint_value, str) or not endpoint_value.strip():
            warn_provider_failure(provider.strip(), "http endpoint is not configured")
            return None
        endpoint = endpoint_value.strip()
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            warn_provider_failure(provider.strip(), "http endpoint must be an http(s) URL")
            return None
    return SemanticRetrievalProvider(
        name=provider.strip(),
        transport=transport,
        command=command,
        endpoint=endpoint,
        timeout_seconds=float(timeout),
        cache_dir=cache_dir_text,
    )


def provider_document_payload(document: Document) -> dict[str, Any]:
    return {
        "path": document.path,
        "scope": document.scope,
        "kind": document.kind,
        "title": document.title,
        "headings": document.headings,
        "source_ids": document.source_ids,
    }


def provider_request_payload(
    project_root: Path,
    config: dict[str, Any],
    scope: str,
    query: str,
    limit: int,
    documents: list[Document],
    semantic_cache_dir: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
        "query": query,
        "scope": scope,
        "limit": limit,
        "project_root": str(project_root),
        "corpus_roots": corpus_roots_for_provider(project_root, config, scope),
        "documents": [provider_document_payload(document) for document in documents],
    }
    if semantic_cache_dir is not None:
        payload["semantic_cache_dir"] = semantic_cache_dir
    return payload


def validate_provider_result_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrievalProviderError("result path must be a non-empty workspace-relative string")
    raw = value.strip().replace("\\", "/")
    parsed = urlparse(raw)
    if "://" in raw or parsed.scheme:
        raise RetrievalProviderError(f"result path must not be a URL: {value}")
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise RetrievalProviderError(f"result path must not be absolute: {value}")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise RetrievalProviderError(f"result path escapes the workspace: {value}")
    return path.as_posix()


def validate_provider_score(value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RetrievalProviderError("result score must be numeric")
    return value


def validate_optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise RetrievalProviderError(f"result {label} must be a string when present")


def validate_optional_string_list(value: Any, label: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise RetrievalProviderError(f"result {label} must be a list of strings when present")


def hydrate_provider_results(
    payload: Any,
    documents: list[Document],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RetrievalProviderError("response must be a JSON object")
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raise RetrievalProviderError("response results must be a list")

    limit = effective_query_limit(limit)
    documents_by_path = {document.path: document for document in documents}
    terms = set(tokenize(query))
    hydrated: list[dict[str, Any]] = []
    for raw_result in raw_results[:limit]:
        if not isinstance(raw_result, dict):
            raise RetrievalProviderError("each result must be an object")
        result_path = validate_provider_result_path(raw_result.get("path"))
        document = documents_by_path.get(result_path)
        if document is None:
            raise RetrievalProviderError(f"result path is not in the query corpus: {result_path}")
        score = validate_provider_score(raw_result.get("score"))
        snippet = validate_optional_string(raw_result.get("snippet"), "snippet")
        provider_headings = validate_optional_string_list(raw_result.get("matched_headings"), "matched_headings")
        hydrated.append(
            {
                "path": document.path,
                "scope": document.scope,
                "kind": document.kind,
                "title": document.title,
                "source_ids": document.source_ids,
                "matched_headings": provider_headings if provider_headings is not None else matched_headings(document, terms),
                "snippet": snippet if snippet is not None else best_snippet(document, terms),
                "score": score,
            }
        )
    return hydrated


def warn_provider_failure(provider: str, reason: str) -> None:
    print(f"warning: retrieval provider '{provider}' failed ({reason}); using lexical fallback", file=sys.stderr)


def query_retrieval_provider(
    project_root: Path,
    config: dict[str, Any],
    scope: str,
    query: str,
    limit: int,
    provider: RetrievalProvider,
) -> tuple[list[dict[str, Any]], int] | None:
    limit = effective_query_limit(limit)
    documents = build_index(project_root, config, scope)
    request = provider_request_payload(project_root, config, scope, query, limit, documents)
    try:
        completed = subprocess.run(  # noqa: S603 - retrieval provider command is explicit config, shell=False
            provider.command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=provider.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        warn_provider_failure(provider.name, f"timed out after {provider.timeout_seconds:g}s")
        return None
    except OSError as exc:
        warn_provider_failure(provider.name, str(exc))
        return None

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        detail = f"exit {completed.returncode}" + (f": {stderr}" if stderr else "")
        warn_provider_failure(provider.name, detail)
        return None

    try:
        payload = json.loads(completed.stdout)
        results = hydrate_provider_results(payload, documents, query, limit)
    except (json.JSONDecodeError, RetrievalProviderError) as exc:
        warn_provider_failure(provider.name, str(exc))
        return None
    return results, len(documents)


def query_semantic_retrieval_provider(
    project_root: Path,
    config: dict[str, Any],
    scope: str,
    query: str,
    limit: int,
    provider: SemanticRetrievalProvider,
) -> tuple[list[dict[str, Any]], int] | None:
    limit = effective_query_limit(limit)
    documents = build_index(project_root, config, scope)
    request = provider_request_payload(project_root, config, scope, query, limit, documents, provider.cache_dir)
    try:
        if provider.transport == "command":
            if provider.command is None:
                raise RetrievalProviderError("command is not configured")
            completed = subprocess.run(  # noqa: S603 - semantic command is explicit config, shell=False
                provider.command,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=provider.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                stderr = completed.stderr.strip()
                detail = f"exit {completed.returncode}" + (f": {stderr}" if stderr else "")
                warn_provider_failure(provider.name, detail)
                return None
            payload = json.loads(completed.stdout)
        else:
            if provider.endpoint is None:
                raise RetrievalProviderError("http endpoint is not configured")
            body = json.dumps(request).encode("utf-8")
            http_request = urllib.request.Request(  # noqa: S310 - opt-in endpoint is validated by the provider config.
                provider.endpoint,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(http_request, timeout=provider.timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        results = hydrate_provider_results(payload, documents, query, limit)
    except subprocess.TimeoutExpired:
        warn_provider_failure(provider.name, f"timed out after {provider.timeout_seconds:g}s")
        return None
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        warn_provider_failure(provider.name, str(exc))
        return None
    except (json.JSONDecodeError, RetrievalProviderError) as exc:
        warn_provider_failure(provider.name, str(exc))
        return None
    return results, len(documents)


def normalized_scores(results: list[dict[str, Any]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    max_score = max((float(result.get("score", 0) or 0) for result in results), default=0.0)
    if max_score <= 0:
        return {str(result["path"]): 0.0 for result in results if isinstance(result.get("path"), str)}
    for result in results:
        path = result.get("path")
        if isinstance(path, str):
            scores[path] = float(result.get("score", 0) or 0) / max_score
    return scores


def merge_headings(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    for heading in [*first, *second]:
        if isinstance(heading, str) and heading not in merged:
            merged.append(heading)
    return merged


def hybrid_rank_results(lexical_results: list[dict[str, Any]], semantic_results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    limit = effective_query_limit(limit)
    lexical_by_path = {str(result["path"]): result for result in lexical_results if isinstance(result.get("path"), str)}
    semantic_by_path = {str(result["path"]): result for result in semantic_results if isinstance(result.get("path"), str)}
    lexical_scores = normalized_scores(lexical_results)
    semantic_scores = normalized_scores(semantic_results)
    paths = sorted(set(lexical_by_path) | set(semantic_by_path))
    merged: list[tuple[float, int, str, dict[str, Any]]] = []
    for path in paths:
        lexical = lexical_by_path.get(path)
        semantic = semantic_by_path.get(path)
        base = dict(semantic or lexical or {})
        if lexical and semantic:
            base["matched_headings"] = merge_headings(
                lexical.get("matched_headings", []),
                semantic.get("matched_headings", []),
            )
            if not semantic.get("snippet") and lexical.get("snippet"):
                base["snippet"] = lexical["snippet"]
        lexical_score = lexical_scores.get(path, 0.0)
        semantic_score = semantic_scores.get(path, 0.0)
        combined = lexical_score + (SEMANTIC_HYBRID_WEIGHT * semantic_score)
        base["lexical_score"] = round(float(lexical.get("score", 0) if lexical else 0), 6)
        base["semantic_score"] = round(float(semantic.get("score", 0) if semantic else 0), 6)
        base["score"] = round(combined, 6)
        scope_rank = SCOPE_TIE_BREAK.get(str(base.get("scope")), 2)
        merged.append((combined, scope_rank, path, base))
    merged.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item for _combined, _scope_rank, _path, item in merged[:limit]]


def write_semantic_cache_artifact(project_root: Path, provider: SemanticRetrievalProvider, query: str, scope: str, indexed: int) -> None:
    cache_dir = project_root / provider.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "schema_version": PROVIDER_REQUEST_SCHEMA_VERSION,
        "provider": provider.name,
        "transport": provider.transport,
        "query": query,
        "scope": scope,
        "indexed_documents": indexed,
    }
    (cache_dir / "last-query.json").write_text(json.dumps(artifact, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def add_engine(results: list[dict[str, Any]], engine: str) -> list[dict[str, Any]]:
    labeled: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        item["engine"] = engine
        labeled.append(item)
    return labeled


def resolve_index_path(project_root: Path, index_path: str | None) -> Path:
    if index_path:
        return project_root / validate_workspace_relative_path(index_path, "index_path")
    return project_root / DEFAULT_INDEX_PATH


def query_index_lock_path(index_path: Path) -> Path:
    return index_path.parent / ".locks" / f"{index_path.name}.lock"


def unique_index_temp_path(index_path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{index_path.name}.",
        suffix=".tmp",
        dir=index_path.parent,
    )
    os.close(descriptor)
    return Path(name)


def cleanup_index_temp_files(tmp_path: Path) -> list[Path]:
    failures: list[Path] = []
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{tmp_path}{suffix}")
        for attempt in range(3):
            try:
                path.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 2:
                    failures.append(path)
                else:
                    time.sleep(0.01)
            except OSError:
                failures.append(path)
                break
    return failures


def sqlite_fts5_available() -> bool:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE VIRTUAL TABLE fts5_check USING fts5(value)")
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()
    return True


def document_content_hash(document: Document) -> str:
    payload = {
        "path": document.path,
        "scope": document.scope,
        "kind": document.kind,
        "title": document.title,
        "headings": document.headings,
        "source_ids": document.source_ids,
        "body": document.body,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_fts_schema(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        """
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            path UNINDEXED,
            scope UNINDEXED,
            kind UNINDEXED,
            title,
            headings,
            source_ids,
            body,
            headings_json UNINDEXED,
            source_ids_json UNINDEXED,
            content_hash UNINDEXED,
            tokenize = 'unicode61'
        )
        """
    )


def write_fts_index(project_root: Path, config: dict[str, Any], scope: str, index_path: Path) -> int:
    if not sqlite_fts5_available():
        raise SystemExit("SQLite FTS5 is required to build the persistent query index.")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    with workspace_lock(query_index_lock_path(index_path), purpose=f"query index build {index_path.name}"):
        documents = build_index(project_root, config, scope)
        tmp_path = unique_index_temp_path(index_path)
        connection: sqlite3.Connection | None = None
        cleanup_failures: list[Path] = []
        try:
            connection = sqlite3.connect(tmp_path)
            create_fts_schema(connection)
            connection.executemany(
                "INSERT INTO metadata (key, value) VALUES (?, ?)",
                [
                    ("schema_version", INDEX_SCHEMA_VERSION),
                    ("scope", scope),
                    ("document_count", str(len(documents))),
                    ("corpus_fingerprint", corpus_fingerprint(project_root, config, scope)),
                ],
            )
            connection.executemany(
                """
                INSERT INTO documents_fts (
                    path,
                    scope,
                    kind,
                    title,
                    headings,
                    source_ids,
                    body,
                    headings_json,
                    source_ids_json,
                    content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        document.path,
                        document.scope,
                        document.kind,
                        document.title,
                        " ".join(document.headings),
                        " ".join(document.source_ids),
                        document.body,
                        json.dumps(document.headings, ensure_ascii=False),
                        json.dumps(document.source_ids, ensure_ascii=False),
                        document_content_hash(document),
                    )
                    for document in documents
                ],
            )
            connection.commit()
            connection.close()
            connection = None
            tmp_path.replace(index_path)
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                cleanup_failures = cleanup_index_temp_files(tmp_path)
        if cleanup_failures:
            names = ", ".join(path.name for path in cleanup_failures)
            raise OSError(f"query index temporary cleanup failed: {names}")
        return len(documents)


def fts_match_query(query: str) -> str:
    terms = tokenize(query)
    return " OR ".join(f"{term}*" for term in terms)


def parse_json_list(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def document_from_fts_row(row: sqlite3.Row) -> Document:
    document = Document(
        path=row["path"],
        scope=row["scope"],
        kind=row["kind"],
        title=row["title"],
        headings=parse_json_list(row["headings_json"]),
        source_ids=parse_json_list(row["source_ids_json"]),
        body=row["body"],
    )
    return prepare_document(document)


def count_fts_documents(connection: sqlite3.Connection, scope: str) -> int:
    if scope == "all":
        row = connection.execute("SELECT COUNT(*) AS count FROM documents_fts").fetchone()
    else:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM documents_fts WHERE scope = ?",
            (scope,),
        ).fetchone()
    return int(row["count"])


def query_fts_index(
    index_path: Path,
    query: str,
    scope: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    limit = effective_query_limit(limit)
    match_query = fts_match_query(query)
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    terms = set(tokenize(query))
    phrase = " ".join(query.lower().split())
    scored: list[tuple[float, int, str, Document]] = []
    try:
        indexed = count_fts_documents(connection, scope)
        if not match_query or limit <= 0:
            return [], indexed
        params: list[Any] = [match_query]
        if scope != "all":
            params.append(scope)
        params.append(limit)
        sql = FTS_SELECT_SQL if scope == "all" else FTS_SELECT_SCOPED_SQL
        rows = connection.execute(sql, params)
        for row in rows:
            document = document_from_fts_row(row)
            score = (-float(row["rank"]) * 1_000_000.0) + supplemental_boost(document, terms, phrase)
            scope_rank = SCOPE_TIE_BREAK.get(document.scope, 2)
            scored.append((score, scope_rank, document.path, document))
    finally:
        connection.close()

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    results: list[dict[str, Any]] = []
    for score, _scope_rank, _path, document in scored[:limit]:
        results.append(
            {
                "path": document.path,
                "scope": document.scope,
                "kind": document.kind,
                "title": document.title,
                "source_ids": document.source_ids,
                "matched_headings": matched_headings(document, terms),
                "snippet": best_snippet(document, terms),
                "score": round(score, 6),
            }
        )
    return results, indexed


def index_metadata(index_path: Path) -> dict[str, str]:
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT key, value FROM metadata").fetchall()
    finally:
        connection.close()
    return {row["key"]: row["value"] for row in rows}


def index_covers_scope(index_scope: str, query_scope: str) -> bool:
    """An index built over all documents covers any query scope."""
    if index_scope == "all":
        return True
    return index_scope == query_scope


def evaluate_index(
    project_root: Path,
    config: dict[str, Any],
    index_path: Path,
    query_scope: str,
) -> tuple[bool, str | None]:
    """Decide whether a persistent FTS index can be trusted for this query.

    Returns ``(usable, note)``. ``note`` is a short stderr-friendly explanation
    when an index exists but must be bypassed. Stale, scope-mismatched, or
    unreadable indexes fall back to the always-correct in-memory scan instead of
    silently returning outdated or partial results.
    """
    if not index_path.is_file():
        return False, None
    if not sqlite_fts5_available():
        return False, "SQLite FTS5 unavailable; using in-memory scan"
    try:
        metadata = index_metadata(index_path)
    except sqlite3.Error:
        return False, "query index is unreadable; using in-memory scan"
    if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
        return False, "query index schema is outdated; using in-memory scan (rebuild with build-index)"
    index_scope = metadata.get("scope")
    if index_scope not in SCOPES:
        return False, "query index scope metadata is invalid; using in-memory scan (rebuild with build-index)"
    if not index_covers_scope(index_scope, query_scope):
        return False, (
            f"query index was built with scope '{index_scope}' but scope '{query_scope}' was requested; "
            "using in-memory scan (rebuild with --scope all)"
        )
    expected = metadata.get("corpus_fingerprint")
    if not expected or corpus_fingerprint(project_root, config, index_scope) != expected:
        return False, "query index is stale; using in-memory scan (rebuild with build-index)"
    return True, None


def query_with_optional_fts(
    project_root: Path,
    config: dict[str, Any],
    scope: str,
    query: str,
    limit: int,
    index_path: Path,
    *,
    warnings: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    limit = effective_query_limit(limit)
    usable, note = evaluate_index(project_root, config, index_path, scope)
    if usable:
        try:
            return query_fts_index(index_path, query, scope, limit)
        except sqlite3.Error:
            note = "query index read failed; using in-memory scan"
    if note:
        print(f"note: {note}", file=sys.stderr)
        if warnings is not None:
            warnings.append(
                {
                    "code": "QUERY_INDEX_FALLBACK",
                    "message": note,
                    "remediation": (
                        "Rebuild with scripts/query_index.py build-index for the required scope, "
                        "then repeat the same query."
                    ),
                }
            )
    documents = build_index(project_root, config, scope)
    return rank_documents(documents, query, limit), len(documents)


def render_text(
    query: str,
    scope: str,
    results: list[dict[str, Any]],
    indexed: int,
    unnormalized: int = 0,
) -> str:
    lines = [
        "Research Index Query",
        "====================",
        f"Query: {query}",
        f"Scope: {scope}",
        f"Indexed documents: {indexed}",
        f"Results: {len(results)}",
        "",
    ]
    if not results:
        lines.append("No matching documents. Broaden the query or ingest more sources.")
        lines.extend(unnormalized_note_lines(unnormalized))
        return "\n".join(lines).rstrip() + "\n"
    for position, result in enumerate(results, start=1):
        source_ids = ", ".join(result["source_ids"]) if result["source_ids"] else "none"
        lines.append(f"{position}. [{result['scope']}/{result['kind']}] {result['title']}")
        lines.append(f"   path: {result['path']}")
        lines.append(f"   score: {result['score']}  source_ids: {source_ids}")
        if result["matched_headings"]:
            lines.append(f"   headings: {', '.join(result['matched_headings'])}")
        if result["snippet"]:
            lines.append(f"   snippet: {result['snippet']}")
        lines.append("")
    lines.extend(unnormalized_note_lines(unnormalized))
    return "\n".join(lines).rstrip() + "\n"


def unnormalized_note_lines(unnormalized: int) -> list[str]:
    if unnormalized <= 0:
        return []
    return [
        "",
        f"Note: {unnormalized} discovered source(s) are not yet normalized and are not "
        "searchable. Run scripts/normalize_sources.py to make them answerable.",
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=getattr(args, "format", None) == "json")
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        config = load_config(project_root)
        index_path = resolve_index_path(project_root, args.index_path)

        if args.command == "build-index":
            indexed = write_fts_index(project_root, config, args.scope, index_path)
            print(f"Indexed {indexed} documents into {index_path}")
            return 0

        query = " ".join(args.query).strip()
        if not query:
            raise SystemExit("Provide one or more query terms.")
        limit = effective_query_limit(args.limit)
        provider = retrieval_provider(config)
        engine = LEXICAL_ENGINE
        provider_result = None
        query_warnings: list[dict[str, str]] = []
        if provider is not None:
            provider_result = query_retrieval_provider(project_root, config, args.scope, query, limit, provider)
        if provider_result is not None:
            results, indexed = provider_result
            engine = provider.name
        else:
            results, indexed = query_with_optional_fts(
                project_root,
                config,
                args.scope,
                query,
                limit,
                index_path,
                warnings=query_warnings,
            )
            semantic_provider = semantic_retrieval_provider(config)
            if semantic_provider is not None:
                semantic_result = query_semantic_retrieval_provider(
                    project_root,
                    config,
                    args.scope,
                    query,
                    limit,
                    semantic_provider,
                )
                if semantic_result is not None:
                    semantic_results, semantic_indexed = semantic_result
                    results = hybrid_rank_results(results, semantic_results, limit)
                    indexed = max(indexed, semantic_indexed)
                    engine = HYBRID_ENGINE
                    write_semantic_cache_artifact(project_root, semantic_provider, query, args.scope, indexed)
        results = add_engine(results, engine)
        results = enrich_related_source_ids(results, citation_relation_graph(project_root, config))
        results = enrich_evidence_links(results, evidence_path_graph(project_root, config))
        unnormalized = unnormalized_source_ids(project_root, config)
    except SystemExit as exc:
        if not json_mode:
            raise
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=2)

    if args.format == "json":
        payload = {
            "query": query,
            "scope": args.scope,
            "engine": engine,
            "indexed_documents": indexed,
            "result_count": len(results),
            "results": results,
            "warnings": query_warnings,
            "unnormalized_source_count": len(unnormalized),
            "unnormalized_source_ids": unnormalized[:MAX_REPORTED_UNNORMALIZED],
        }
        print(json.dumps(payload, indent=2, sort_keys=False))
    else:
        sys.stdout.write(render_text(query, args.scope, results, indexed, len(unnormalized)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
