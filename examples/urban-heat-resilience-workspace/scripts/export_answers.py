#!/usr/bin/env python3
"""Export structured question answers for downstream agents.

This is the machine export half of the question lifecycle API. It turns the
question backlog into a versioned JSON document so downstream consumers get
answer summaries, answer-page locations, and cited evidence with provenance
without parsing wiki Markdown.

Per-question record fields:

- ``slug``, ``question``, ``status``, ``priority``, ``origin``
- ``question_page``: workspace-relative path to the question task page
- ``answer_page``: workspace-relative path to the linked answer page (null
  until answered; the raw frontmatter value is kept when it does not resolve,
  with a warning)
- ``answer_summary``: ``summary`` frontmatter of the answer page, or its first
  body paragraph
- ``source_ids``: union of question-page and answer-page ``source_ids``
- ``grounding[]`` and ``grounding_verification``: quote-anchored claim support
  from question frontmatter plus deterministic verification against normalized
  records
- ``citations[]``: one entry per source id with ``source_id``, ``in_manifest``,
  manifest ``raw_paths``, ``normalized_record`` path when one exists, ``title``
  from the normalized record, and provenance ``origin_url``/``license``/
  ``checksum``/``checksum_verified`` when present on the manifest record
- ``blocked_reason`` for blocked questions
- ``confidence`` and ``evidence_strength`` only when the question page carries
  them (set by the verification pass, ``skills/research-verify.md``)
- coverage fields from ``coverage_manifest.py``, including facet-level
  ``claim_probe`` metadata and flattened ``unconfirmed_claims`` when present
- audit fields: flattened ``policy_results``, currentness policy results,
  local citation verification results, and selected/fetched discovery candidate
  traces linked to the question's source ids

Envelope fields: ``schema_version``, ``generated_at``, ``project`` (name plus
``handoff`` passthrough), backlog ``counts``, applied ``filters``, and
``warnings`` (missing answer pages or unknown source ids surface here, never
as crashes).

Output formats:

- ``json`` (default): one document with a ``questions`` array.
- ``jsonl``: first line is the envelope (``record_type: envelope``), then one
  line per question (``record_type: question``).

The script is read-only. Output ordering is deterministic for a fixed
workspace (status, then priority, then slug).

Exit codes:

- ``0``: export produced (warnings allowed).
- ``2``: unreadable workspace (missing or invalid ``research.yml``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required to export question answers") from exc


SCHEMA_VERSION = "1.0"
EXIT_OK = 0
EXIT_UNREADABLE = 2

_SIBLING_CACHE: dict[str, ModuleType] = {}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _handoff_signature import project_handoff_verification
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_module_loader import load_workspace_module


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export structured question answers with citations for downstream agents.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root containing research.yml. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "jsonl"),
        default="json",
        help="Output format. Defaults to json.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write the export to this path instead of stdout.",
    )
    parser.add_argument(
        "--status",
        action="append",
        default=None,
        help="Only export questions with the given status. Repeatable. Default: all statuses.",
    )
    return parser.parse_args(argv)


def load_sibling_module(stem: str) -> ModuleType:
    """Load a sibling workspace script as a module so its logic is reused directly."""
    if stem not in _SIBLING_CACHE:
        _SIBLING_CACHE[stem] = load_workspace_module(_SCRIPT_DIR, stem)
    return _SIBLING_CACHE[stem]


def load_config(project_root: Path) -> dict[str, Any]:
    config_path = project_root / "research.yml"
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SystemExit(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise SystemExit(f"Invalid config: {config_path}")
    return config


def split_page(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split a wiki page into (frontmatter mapping, body text)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end == -1:
        return None, text
    block = text[4:end]
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :] if body_start != -1 else ""
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None, body
    return (data if isinstance(data, dict) else None), body


def first_paragraph(body: str) -> str:
    for block in body.split("\n\n"):
        lines = [line.strip() for line in block.strip().splitlines() if line.strip()]
        text_lines = [line for line in lines if not line.startswith("#")]
        if text_lines:
            return " ".join(" ".join(text_lines).split())
    return ""


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def text_field(frontmatter: dict[str, Any], key: str) -> str:
    value = frontmatter.get(key)
    return value.strip() if isinstance(value, str) else ""


def workspace_relative(path: Path, project_root: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_root).as_posix()
    except ValueError:
        return None


def load_manifest_records(project_root: Path, config: dict[str, Any], warnings: list[str]) -> dict[str, dict[str, Any]]:
    normalize = load_sibling_module("normalize_sources")
    try:
        manifest_rel, _ = normalize.source_paths(config)
    except SystemExit as exc:
        warnings.append(f"Cannot resolve manifest path: {exc}")
        return {}
    manifest_path = project_root / manifest_rel
    if not manifest_path.is_file():
        warnings.append(f"Manifest not found: {manifest_rel}")
        return {}
    records: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"Invalid manifest record skipped: {manifest_rel}:{line_number}")
            continue
        if not isinstance(record, dict):
            warnings.append(f"Invalid manifest record skipped: {manifest_rel}:{line_number}")
            continue
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id and record_id not in records:
            records[record_id] = record
    return records


def evidence_usability_override_summary(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    source_ids: list[str] = []
    for source_id, record in sorted(records.items()):
        provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
        if isinstance(provenance.get("evidence_usability_override"), dict):
            source_ids.append(source_id)
    return {"count": len(source_ids), "source_ids": source_ids}


def normalized_record_lookup(project_root: Path, config: dict[str, Any], source_id: str) -> tuple[str | None, str | None]:
    """Return (workspace-relative normalized record path, title) when available."""
    normalize = load_sibling_module("normalize_sources")
    _, normalized_rel = normalize.source_paths(config)
    record_path = project_root / normalized_rel / f"{normalize.safe_source_id(source_id)}.md"
    if not record_path.is_file():
        return None, None
    frontmatter, _ = split_page(record_path.read_text(encoding="utf-8"))
    title = None
    if isinstance(frontmatter, dict):
        value = frontmatter.get("title")
        if isinstance(value, str) and value.strip():
            title = value.strip()
    return f"{normalized_rel}/{record_path.name}", title


def academic_citation_metadata(provenance: dict[str, Any]) -> dict[str, Any] | None:
    field_map = {
        "academic_provider": "provider",
        "academic_source_type": "source_type",
        "venue": "venue",
        "publication_year": "publication_year",
        "oa_status": "oa_status",
        "peer_review_status": "peer_review_status",
        "arxiv_id": "arxiv_id",
        "openalex_work_id": "openalex_work_id",
        "doi": "doi",
    }
    academic: dict[str, Any] = {}
    for source_key, target_key in field_map.items():
        value = provenance.get(source_key)
        if isinstance(value, str) and value.strip():
            academic[target_key] = value.strip()
        elif source_key == "publication_year" and isinstance(value, int) and not isinstance(value, bool):
            academic[target_key] = value
    if "provider" not in academic:
        return None
    return academic


def standards_citation_metadata(provenance: dict[str, Any]) -> dict[str, Any] | None:
    standards = provenance.get("standards")
    if not isinstance(standards, dict):
        return None
    return dict(standards)


def build_citation(
    source_id: str,
    manifest: dict[str, dict[str, Any]],
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    warnings: list[str],
) -> dict[str, Any]:
    record = manifest.get(source_id)
    citation: dict[str, Any] = {
        "source_id": source_id,
        "in_manifest": record is not None,
        "raw_paths": [],
        "normalized_record": None,
        "title": None,
        "origin_url": None,
        "license": None,
    }
    if record is None:
        warnings.append(f"Question '{slug}' cites source_id not in manifest: {source_id}")
        return citation
    citation["raw_paths"] = string_list(record.get("raw_paths"))
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    origin_url = provenance.get("origin_url")
    if not (isinstance(origin_url, str) and origin_url.strip()):
        origin_url = record.get("url")
    if isinstance(origin_url, str) and origin_url.strip():
        citation["origin_url"] = origin_url.strip()
    license_value = provenance.get("license")
    if isinstance(license_value, str) and license_value.strip():
        citation["license"] = license_value.strip()
    checksum_value = provenance.get("checksum")
    if isinstance(checksum_value, str) and checksum_value.strip():
        citation["checksum"] = checksum_value.strip()
    if isinstance(provenance.get("checksum_verified"), bool):
        citation["checksum_verified"] = provenance["checksum_verified"]
    academic = academic_citation_metadata(provenance)
    if academic is not None:
        citation["academic"] = academic
    standards = standards_citation_metadata(provenance)
    if standards is not None:
        citation["standards"] = standards
    for field in ("source_type", "jurisdiction", "publisher", "curation_notes"):
        value = provenance.get(field)
        if isinstance(value, str) and value.strip():
            citation[field] = value.strip()
    date_metadata = provenance.get("date_metadata")
    if isinstance(date_metadata, dict):
        citation["date_metadata"] = dict(date_metadata)
    override = provenance.get("evidence_usability_override")
    if isinstance(override, dict):
        citation["evidence_usability_override"] = dict(override)
    evidence_areas = provenance.get("supported_evidence_areas")
    if isinstance(evidence_areas, list):
        citation["supported_evidence_areas"] = [
            item.strip() for item in evidence_areas if isinstance(item, str) and item.strip()
        ]
    normalized_record, title = normalized_record_lookup(project_root, config, source_id)
    citation["normalized_record"] = normalized_record
    citation["title"] = title
    return citation


def load_json_file(path: Path, warnings: list[str]) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        warnings.append(f"Invalid JSON skipped: {path.as_posix()}")
        return {}
    return document if isinstance(document, dict) else {}


def candidate_store_path(project_root: Path) -> Path:
    return project_root / "sources" / "discovery" / "candidates.jsonl"


def load_candidate_records(project_root: Path, warnings: list[str]) -> list[dict[str, Any]]:
    path = candidate_store_path(project_root)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            warnings.append(f"Invalid candidate record skipped: sources/discovery/candidates.jsonl:{line_number}")
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_source_request_records(project_root: Path, config: dict[str, Any], warnings: list[str]) -> dict[str, dict[str, Any]]:
    source_requests = load_sibling_module("source_requests")
    try:
        path = source_requests.requests_path(project_root, config)
        requests = source_requests.load_requests(path)
    except SystemExit as exc:
        warnings.append(f"Cannot load source requests: {exc}")
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    for record in requests:
        request_id = record.get("request_id")
        if isinstance(request_id, str) and request_id.strip() and request_id not in by_id:
            by_id[request_id.strip()] = record
    return by_id


def source_request_summary(record: dict[str, Any]) -> dict[str, Any]:
    title = record.get("title")
    summary = record.get("summary")
    query = record.get("query_or_identifier")
    rationale = record.get("rationale")
    title_text = title.strip() if isinstance(title, str) and title.strip() else None
    summary_text = summary.strip() if isinstance(summary, str) and summary.strip() else title_text
    if summary_text is None and isinstance(rationale, str) and rationale.strip():
        summary_text = rationale.strip()
    evidence_area = record.get("evidence_area")
    if evidence_area is None:
        evidence_area = record.get("requested_evidence_area")
    if evidence_area is None:
        evidence_area = record.get("evidence_areas")
    return {
        "request_id": record.get("request_id"),
        "title": title_text,
        "summary": summary_text,
        "status": record.get("status"),
        "question_slugs": string_list(record.get("question_slugs")),
        "evidence_area": evidence_area,
        "query_or_identifier": query.strip() if isinstance(query, str) and query.strip() else None,
        "rationale": rationale.strip() if isinstance(rationale, str) and rationale.strip() else None,
        "source_id": record.get("source_id"),
    }


def candidate_selected_request_id(candidate: dict[str, Any]) -> str | None:
    for key in ("selected_for_request_id", "selected_request_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_selected_source_id(candidate: dict[str, Any]) -> str | None:
    for key in ("selected_source_id", "manifest_source_id", "source_id", "fetched_source_id"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def candidate_references_source(candidate: dict[str, Any], source_id: str) -> bool:
    if candidate_selected_source_id(candidate) == source_id:
        return True
    source_ids = candidate.get("source_ids")
    return isinstance(source_ids, list) and source_id in source_ids


def candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "status": candidate.get("status"),
        "source_type": candidate.get("source_type"),
        "evidence_path": candidate.get("evidence_path"),
        "trust_tier": candidate.get("trust_tier"),
        "recommended_action": candidate.get("recommended_action"),
        "selected_for_request_id": candidate_selected_request_id(candidate),
        "selected_source_id": candidate_selected_source_id(candidate),
        "url": candidate.get("url"),
        "title": candidate.get("title"),
    }


def candidate_trace_for_sources(source_ids: list[str], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    for candidate in candidates:
        if any(candidate_references_source(candidate, source_id) for source_id in source_ids):
            trace.append(candidate_summary(candidate))
    trace.sort(key=lambda item: str(item.get("candidate_id") or ""))
    return trace


def citation_verification_report_path(project_root: Path) -> Path | None:
    direct = project_root / "sources" / "citation-verification.json"
    if direct.is_file():
        return direct
    run_reports = sorted((project_root / "runs").glob("*/evaluation/citation-verification.json"))
    return run_reports[-1] if run_reports else None


def load_citation_verification_by_source(project_root: Path, warnings: list[str]) -> dict[str, list[dict[str, Any]]]:
    path = citation_verification_report_path(project_root)
    if path is None:
        return {}
    report = load_json_file(path, warnings)
    results = report.get("results") if isinstance(report.get("results"), list) else []
    by_source: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        source_id = result.get("source_id")
        if isinstance(source_id, str) and source_id.strip():
            by_source.setdefault(source_id.strip(), []).append(dict(result))
    return by_source


def citation_verification_for_sources(
    source_ids: list[str], by_source: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for source_id in source_ids:
        results.extend(by_source.get(source_id, []))
    return results


def currentness_results(policy_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        result
        for result in policy_results
        if isinstance(result.get("policy"), str) and result["policy"].startswith("current_")
    ]


def flattened_policy_results(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    flattened: list[dict[str, Any]] = []
    facets = value.get("facets") if isinstance(value.get("facets"), list) else []
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        results = facet.get("policy_results") if isinstance(facet.get("policy_results"), list) else []
        for result in results:
            if not isinstance(result, dict):
                continue
            item = dict(result)
            item.setdefault("facet_id", facet.get("facet_id"))
            item.setdefault("required", facet.get("required"))
            item.setdefault("evidence_path", facet.get("evidence_path"))
            flattened.append(item)
    return flattened


def human_review_state(frontmatter: dict[str, Any], policy_results: list[dict[str, Any]]) -> dict[str, Any]:
    manual_policies = sorted(
        {
            str(result.get("policy"))
            for result in policy_results
            if isinstance(result, dict)
            and (
                result.get("verdict") == "manual_review"
                or result.get("policy") in {"manual_review_required", "manual_review"}
            )
        }
    )
    required = frontmatter.get("human_review_required") is True or bool(manual_policies)
    approved = frontmatter.get("human_review_approved") is True or text_field(frontmatter, "human_review_status") == "approved"
    status = "approved" if approved else "pending" if required else "not_required"
    return {
        "required": required,
        "status": status,
        "pending": required and not approved,
        "reviewer": text_field(frontmatter, "approved_by") or None,
        "approved_at": text_field(frontmatter, "approved_at") or None,
        "policies": manual_policies or string_list(frontmatter.get("human_review_policies")),
    }


def resolve_answer(
    question_path: Path,
    frontmatter: dict[str, Any],
    project_root: Path,
    slug: str,
    warnings: list[str],
) -> tuple[str | None, str | None, list[str]]:
    """Return (answer_page, answer_summary, answer source_ids)."""
    raw_value = text_field(frontmatter, "answer_page")
    if not raw_value:
        return None, None, []
    target = (question_path.parent / raw_value).resolve()
    relative = workspace_relative(target, project_root)
    if relative is None or not target.is_file():
        warnings.append(f"Question '{slug}' links a missing answer page: {raw_value}")
        return raw_value, None, []
    answer_frontmatter, body = split_page(target.read_text(encoding="utf-8"))
    summary = ""
    source_ids: list[str] = []
    if isinstance(answer_frontmatter, dict):
        summary = text_field(answer_frontmatter, "summary")
        source_ids = string_list(answer_frontmatter.get("source_ids"))
    if not summary:
        summary = first_paragraph(body)
    return relative, summary or None, source_ids


def grounding_for_question(
    question_path: Path,
    frontmatter: dict[str, Any],
    project_root: Path,
    config: dict[str, Any],
    slug: str,
    warnings: list[str],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    verify_quotes = load_sibling_module("verify_quotes")
    try:
        entries = verify_quotes.grounding_entries(frontmatter, slug)
        verification = verify_quotes.verify_question(
            project_root,
            config,
            slug,
            frontmatter=frontmatter,
            path=question_path,
        )
    except verify_quotes.VerifyQuotesError as exc:
        warnings.append(f"Question '{slug}' has invalid grounding: {exc}")
        return [], {
            "slug": slug,
            "question_page": workspace_relative(question_path, project_root) or question_path.name,
            "grounding_count": 0,
            "all_verified": False,
            "grounding": [],
            "error_code": exc.error_code,
            "message": str(exc),
        }
    return entries, verification


def build_question_record(
    question_path: Path,
    frontmatter: dict[str, Any],
    project_root: Path,
    config: dict[str, Any],
    manifest: dict[str, dict[str, Any]],
    candidates: list[dict[str, Any]],
    source_requests_by_id: dict[str, dict[str, Any]],
    citation_verification_by_source: dict[str, list[dict[str, Any]]],
    warnings: list[str],
) -> dict[str, Any]:
    slug = question_path.stem
    status = frontmatter.get("status")
    answer_page, answer_summary, answer_source_ids = resolve_answer(
        question_path, frontmatter, project_root, slug, warnings
    )
    combined_ids = sorted(set(string_list(frontmatter.get("source_ids")) + answer_source_ids))
    citations = [
        build_citation(source_id, manifest, project_root, config, slug, warnings)
        for source_id in combined_ids
    ]
    blocking_request_ids = string_list(frontmatter.get("blocking_request_ids"))
    blocking_requests = [
        source_request_summary(source_requests_by_id[request_id])
        for request_id in blocking_request_ids
        if request_id in source_requests_by_id
    ]
    missing_blocking_request_ids = [
        request_id for request_id in blocking_request_ids if request_id not in source_requests_by_id
    ]
    grounding, grounding_verification = grounding_for_question(
        question_path,
        frontmatter,
        project_root,
        config,
        slug,
        warnings,
    )
    record: dict[str, Any] = {
        "slug": slug,
        "question": text_field(frontmatter, "question") or text_field(frontmatter, "summary"),
        "status": status if isinstance(status, str) else "unknown",
        "priority": text_field(frontmatter, "priority"),
        "origin": text_field(frontmatter, "origin"),
        "question_page": workspace_relative(question_path, project_root) or question_path.name,
        "answer_page": answer_page,
        "answer_summary": answer_summary,
        "source_ids": combined_ids,
        "grounding": grounding,
        "grounding_verification": grounding_verification,
        "citations": citations,
        "blocked_reason": text_field(frontmatter, "blocked_reason") or None,
        "blocking_request_ids": blocking_request_ids,
        "blocking_requests": blocking_requests,
        "missing_blocking_request_ids": missing_blocking_request_ids,
    }
    coverage = load_sibling_module("coverage_manifest")
    record.update(coverage.coverage_summary_for_question(project_root, config, slug, frontmatter))
    policy_results = flattened_policy_results(record.get("policy_results"))
    record["policy_results"] = policy_results
    record["currentness"] = currentness_results(policy_results)
    record["human_review"] = human_review_state(frontmatter, policy_results)
    record["candidate_trace"] = candidate_trace_for_sources(combined_ids, candidates)
    record["citation_verification"] = citation_verification_for_sources(combined_ids, citation_verification_by_source)
    confidence = text_field(frontmatter, "confidence")
    if confidence:
        record["confidence"] = confidence
    evidence_strength = text_field(frontmatter, "evidence_strength")
    if evidence_strength:
        record["evidence_strength"] = evidence_strength
    return record


def build_export(project_root: Path, status_filter: list[str] | None) -> dict[str, Any]:
    project_root = Path(project_root).expanduser().resolve()
    config = load_config(project_root)
    question_status = load_sibling_module("question_status")
    questions_dir = question_status.questions_directory(project_root, config)

    warnings: list[str] = []
    manifest = load_manifest_records(project_root, config, warnings)
    candidates = load_candidate_records(project_root, warnings)
    source_requests_by_id = load_source_request_records(project_root, config, warnings)
    citation_verification_by_source = load_citation_verification_by_source(project_root, warnings)

    records: list[dict[str, Any]] = []
    if questions_dir.is_dir():
        for path in sorted(questions_dir.glob("*.md")):
            frontmatter = question_status.load_frontmatter(path)
            if frontmatter is None or frontmatter.get("type") != "question":
                continue
            records.append(
                build_question_record(
                    path,
                    frontmatter,
                    project_root,
                    config,
                    manifest,
                    candidates,
                    source_requests_by_id,
                    citation_verification_by_source,
                    warnings,
                )
            )
    records.sort(key=question_status.status_sort_key)

    by_status: dict[str, int] = {}
    for record in records:
        by_status[record["status"]] = by_status.get(record["status"], 0) + 1
    ordered_by_status = {
        status: by_status[status] for status in question_status.STATUS_ORDER if status in by_status
    }
    for status in sorted(by_status):
        ordered_by_status.setdefault(status, by_status[status])

    exported = records
    if status_filter:
        wanted = {value.strip() for value in status_filter if value and value.strip()}
        exported = [record for record in records if record["status"] in wanted]

    project = config.get("project") if isinstance(config.get("project"), dict) else {}
    handoff = project.get("handoff")
    verification = project_handoff_verification(project_root, project)
    if verification.error_code is not None:
        exc = SystemExit(verification.message or "Handoff signature verification failed.")
        exc.error_code = verification.error_code
        exc.details = verification.details or {}
        raise exc
    try:
        questions_dir_label = questions_dir.relative_to(project_root).as_posix()
    except ValueError:
        questions_dir_label = questions_dir.as_posix()

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": {
            "name": project.get("name") if isinstance(project.get("name"), str) else None,
            "handoff": handoff if isinstance(handoff, dict) else None,
        },
        "questions_dir": questions_dir_label,
        "counts": {
            "total": len(records),
            "by_status": ordered_by_status,
            "exported": len(exported),
        },
        "evidence_usability_overrides": evidence_usability_override_summary(manifest),
        "filters": {"status": sorted({value.strip() for value in status_filter}) if status_filter else None},
        "warnings": warnings,
        "questions": exported,
    }


def render_output(document: dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(document, indent=2, sort_keys=False) + "\n"
    envelope = {key: value for key, value in document.items() if key != "questions"}
    envelope["record_type"] = "envelope"
    lines = [json.dumps(envelope, sort_keys=False)]
    for record in document["questions"]:
        line_record = {"record_type": "question", **record}
        lines.append(json.dumps(line_record, sort_keys=False))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=True)
    project_root = Path(args.project_root).expanduser().resolve()
    try:
        document = build_export(project_root, args.status)
    except SystemExit as exc:
        return handle_system_exit(
            exc,
            json_mode=json_mode,
            default_exit_code=EXIT_UNREADABLE,
            error_code=getattr(exc, "error_code", None),
            details=getattr(exc, "details", None),
        )
    rendered = render_output(document, args.format)
    if args.output:
        Path(args.output).expanduser().resolve().write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
