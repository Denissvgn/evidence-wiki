#!/usr/bin/env python3
"""Evaluate selected questions against one immutable local workspace capture."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from _evidence_revision import MAX_ATTEMPTS, capture_workspace, content_id, refuse
from _publication_context import authorized_capture
from _workspace_module_loader import load_workspace_module

SCHEMA_VERSION = "evidence-selected-publication/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
_STRICT_CACHE = {}
GLOBAL_GATES = (
    "configuration_and_workspace_health", "source_integrity_and_normalization",
    "source_requests", "candidate_lifecycle", "claims_and_contradictions",
    "license_and_curation", "secrets_and_retained_outputs",
)
SELECTED_GATES = ("question_lifecycle_and_review", "grounding_and_citations", "coverage")
BLOCKING_SOURCE_CATEGORIES = frozenset({
    "source_missing_normalized", "normalized_orphan", "normalized_record_contract_violation",
})


def normalize_selection(question_slugs: Any) -> tuple[str, ...]:
    """Collapse duplicate slugs into a sorted nonempty set with bounded size."""
    if not isinstance(question_slugs, (list, tuple, set, frozenset)) or not 1 <= len(question_slugs) <= 1000:
        raise refuse("PUBLICATION_SELECTION_INVALID", "Provide between one and 1000 question slugs.")
    if any(not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", slug) for slug in question_slugs):
        raise refuse("PUBLICATION_SELECTION_INVALID", "Question slugs must be portable filename stems.")
    return tuple(sorted(set(question_slugs)))


def validate_config_paths(config: dict[str, Any]) -> None:
    """Prevent configured local readers from escaping the captured workspace."""
    visited = 0
    def visit(value: Any, keys: tuple[str, ...] = (), depth: int = 0, ancestors: frozenset[int] = frozenset()) -> None:
        nonlocal visited
        visited += 1
        if depth > 64 or visited > 200_000 or isinstance(value, (dict, list)) and id(value) in ancestors:
            raise refuse("PUBLICATION_CONFIG_INVALID", "Configuration exceeds the nesting bound.")
        descendants = ancestors | {id(value)}
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise refuse("PUBLICATION_CONFIG_INVALID", "Configuration keys must be strings.")
                visit(child, (*keys, key), depth + 1, descendants)
        elif isinstance(value, list):
            for child in value:
                visit(child, keys, depth + 1, descendants)
        elif isinstance(value, str) and keys:
            key = keys[-1]
            if key in {"path", "root", "required_dirs"} or key.endswith(("_path", "_dir", "_root", "_roots", "_file")):
                parts = PurePosixPath(value).parts
                if not value or value.startswith("/") or ".." in parts or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
                    raise refuse("PUBLICATION_CONFIG_INVALID", "A configured path is not workspace-relative.", field=".".join(keys))
    visit(config)


def producer_identity() -> str:
    """Bind the trusted script implementation independently of workspace copies."""
    return content_id("evidence-publication-producer/v1", [
        {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(SCRIPT_DIR.glob("*.py"))
    ])


def logical_paths(value: Any, temporary_root: Path) -> Any:
    """Keep reports independent of the private materialization's random path."""
    if isinstance(value, dict):
        return {key: logical_paths(child, temporary_root) for key, child in value.items()}
    if isinstance(value, list):
        return [logical_paths(child, temporary_root) for child in value]
    if isinstance(value, str):
        return value.replace(str(temporary_root), ".")
    return value


def validate_before_materialization(files: Any, readiness: Any, *, usage_view=None, purpose=None, consumer=None, expected_config=None) -> tuple[dict[str, Any], Any]:
    """Validate config and detect known secret forms before copying retained bytes."""
    try:
        config = yaml.safe_load(files.get("research.yml", b""))
    except (ValueError, RecursionError, yaml.YAMLError) as error:
        raise refuse("PUBLICATION_CONFIG_INVALID", "research.yml is not readable YAML.") from error
    if not isinstance(config, dict):
        raise refuse("PUBLICATION_CONFIG_INVALID", "research.yml must contain a mapping.")
    validate_config_paths(config)
    usage = load_workspace_module(SCRIPT_DIR, "_usage_gate")
    inputs = None
    if usage_view is None:
        usage.require_legacy_export(config)
        if any(usage.bytes_have_claims(name, data, config) for name, data in files.items()):
            raise usage.refusal("explicit_usage_requires_host_authorization")
    else:
        if config != expected_config:
            raise usage.refusal("assessment_configuration_changed")
        qualification = load_workspace_module(SCRIPT_DIR, "_publication_usage")
        try:
            inputs = qualification.qualify_capture(files, config, usage_view, purpose=purpose, consumer=consumer)
        except qualification.EvidenceInvalid as exc:
            raise usage.refusal(str(exc)) from exc
    reasons = readiness.empty_reasons()
    for name, data in files.items():
        if Path(name).parts[0] in readiness.SCAN_ROOTS and Path(name).suffix.lower() in readiness.SECRET_SCAN_SUFFIXES:
            if readiness.scan_text_for_secrets(name, data.decode("utf-8", errors="ignore"), reasons):
                raise refuse("PUBLICATION_SAFETY_REFUSED", "Retained content matches the publication secret guard.", path=name)
    return config, inputs


def enforce_global_source_gates(lint: dict[str, Any], root: Path, config: dict[str, Any]) -> None:
    """Make incomplete active normalization a blocking publication defect."""
    sources = config.get("sources")
    manifest_value = sources.get("manifest_path", "sources/manifest.jsonl") if isinstance(sources, dict) else "sources/manifest.jsonl"
    manifest = root / (manifest_value if isinstance(manifest_value, str) else "sources/manifest.jsonl")
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # The owning lint pass already reports malformed records.
            if not isinstance(record, dict) or not isinstance(record.get("raw_paths"), list):
                continue
            for relative in record["raw_paths"]:
                safe = isinstance(relative, str) and bool(relative) and not PurePosixPath(relative).is_absolute() and ".." not in PurePosixPath(relative).parts and "\\" not in relative and ":" not in relative and all(ord(char) >= 32 for char in relative)
                if not safe or not (root / relative).exists():
                    lint.setdefault("issues", []).append({
                        "severity": "HIGH", "category": "publication_raw_input_missing",
                        "message": "A source declares an unavailable or unsafe raw input.",
                        "paths": [str(manifest.relative_to(root))], "source_id": record.get("id"),
                        "remediation": "Restore the declared local raw evidence before publication.",
                    })
    for issue in lint.get("issues", []):
        if issue.get("category") in BLOCKING_SOURCE_CATEGORIES:
            issue["severity"] = "HIGH"
    lint["issue_counts"] = {severity: sum(issue.get("severity") == severity for issue in lint.get("issues", [])) for severity in ("HIGH", "MEDIUM", "LOW")}


def run_selected_publication(
    project_root: Path, question_slugs: Any, *, expected_revision: str | None = None,
    _usage_view=None, _purpose=None, _consumer=None, _expected_config=None,
) -> dict[str, Any]:
    """Return scoped readiness and answers; concurrent changes retry or refuse."""
    selected = normalize_selection(question_slugs)
    if expected_revision is not None and (not isinstance(expected_revision, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_revision)):
        raise refuse("EVIDENCE_REVISION_INVALID", "Expected revision must be a canonical sha256 identity.")
    root = Path(project_root).expanduser().resolve()
    producer = producer_identity()
    readiness = load_workspace_module(SCRIPT_DIR, "publication_readiness")
    questions = load_workspace_module(SCRIPT_DIR, "question_status")
    status_module = load_workspace_module(SCRIPT_DIR, "workspace_status")
    lint_module = load_workspace_module(SCRIPT_DIR, "lint")
    export_module = load_workspace_module(SCRIPT_DIR, "export_answers")
    if producer_identity() != producer:
        raise refuse("EVIDENCE_REVISION_CHANGED", "Publication implementation changed while loading.")
    for _attempt in range(MAX_ATTEMPTS):
        revision = capture_workspace(root)
        strict = load_workspace_module(SCRIPT_DIR, "_strict_evidence", cache=_STRICT_CACHE)
        try:
            candidate_config = yaml.safe_load(revision.files.get("research.yml", b""))
        except yaml.YAMLError:
            candidate_config = None  # The owning config validation below emits the refusal.
        if strict.configured(root, candidate_config):
            return strict.publication(root, selected, view=_usage_view, expected_revision=expected_revision)
        if expected_revision is not None and revision.revision_id != expected_revision:
            raise refuse("EVIDENCE_REVISION_CHANGED", "Workspace no longer matches the expected revision.", expected_revision=expected_revision, actual_revision=revision.revision_id)
        config, inputs = validate_before_materialization(revision.files, readiness, usage_view=_usage_view,
                                                        purpose=_purpose, consumer=_consumer, expected_config=_expected_config)
        with revision.materialize() as captured_root, (
            authorized_capture(captured_root, config, _usage_view) if _usage_view is not None else nullcontext()
        ), (
            strict.internal_publication(captured_root, config)
            if not strict.configured(root, config) and config.get("strict_evidence") is not None else nullcontext()
        ):
            question_dir = questions.questions_directory(captured_root, config)
            known = {item["slug"] for item in questions.collect_questions(question_dir)}
            unknown = sorted(set(selected) - known)
            if unknown:
                raise refuse("PUBLICATION_QUESTION_UNKNOWN", "Selection contains unknown questions.", question_slugs=unknown)
            slugs = frozenset(selected)
            question_paths = frozenset(question_dir / f"{slug}.md" for slug in selected)
            status = status_module.build_status_document(captured_root, question_slugs=slugs)
            lint = lint_module.run_checks(captured_root, config, question_paths=question_paths)
            enforce_global_source_gates(lint, captured_root, config)
            answers = export_module.build_export(captured_root, None, question_slugs=slugs)
            report = readiness.build_readiness_document(captured_root, embedded_inputs={"status": status, "lint": lint, "export": answers})
            document = logical_paths({
                "schema_version": SCHEMA_VERSION,
                "question_slugs": list(selected),
                "duplicate_handling": "sorted_set",
                "revision": revision.describe(),
                "producer_id": producer,
                "gate_scope": {"global": list(GLOBAL_GATES), "selected_questions": list(SELECTED_GATES), "unrelated_source_defects": "blocking"},
                "verdict": report["verdict"],
                "readiness": report,
                "export": answers,
            }, captured_root)
            if inputs is not None:
                document["authorized_inputs"] = inputs
        if capture_workspace(root).revision_id == revision.revision_id and producer_identity() == producer:
            return document
    raise refuse("EVIDENCE_REVISION_CHANGED", "Workspace or publication implementation changed during evaluation.")
