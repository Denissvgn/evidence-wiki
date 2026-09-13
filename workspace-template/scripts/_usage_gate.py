#!/usr/bin/env python3
"""Shared consumer decisions for explicit permissions and host-owned revisions."""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

import yaml
from _evidence_authority import EvidenceInvalid, digest
from _evidence_revision import observation, read_observed_file
from _evidence_usage import CLAIM_KEYS, UsageView, configured, current_view
from _publication_context import captured_config, captured_view
from _record_artifacts import artifact_path
from _script_errors import ScriptRefusal
from _windows_files import read_legacy_file
from _yaml_safe import safe_load


class UsageRefusal(ScriptRefusal, SystemExit):
    """Carry a typed refusal across independently loaded script families."""


def refusal(reason: str) -> ScriptRefusal:
    return UsageRefusal("EVIDENCE_USAGE_REFUSED", "The requested evidence use is not authorized.",
                         exit_code=2, details={"reason": reason},
                         remediation="Provide current host authorization for the exact sanitized revision and requested use.")


def documents_with_metadata(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for document in documents:
        if isinstance(document, dict):
            result.append(document)
            for key in ("metadata", "provenance"):
                if isinstance(document.get(key), dict):
                    result.append(document[key])
    return result


def claims(documents: list[dict[str, Any]], uses: list[str]) -> tuple[list[str], str | None, bool]:
    reasons, revisions = [], set()
    present = False
    for document in documents_with_metadata(documents):
        present |= bool(CLAIM_KEYS & document.keys())
        for use in uses:
            key = use + "_eligible"
            if key in document and document[key] is not True:
                reasons.append("usage_permission_denied" if document[key] is False else "usage_requires_explicit_booleans")
        if "usage_revision_id" in document:
            try:
                revisions.add(digest(document["usage_revision_id"]))
            except EvidenceInvalid:
                reasons.append("invalid_usage_revision_claim")
        # Workspace policy labels never supply authority, even if they look signed.
        if "usage_policy" in document:
            reasons.append("workspace_usage_policy_not_authoritative")
    if len(revisions) > 1:
        reasons.append("conflicting_usage_revision_claims")
    return sorted(set(reasons)), next(iter(revisions)) if len(revisions) == 1 else None, present


def requires_authority(documents: list[dict[str, Any]]) -> bool:
    """An opted-in market profile cannot acquire permissive legacy consumer defaults."""
    return any(value.get("kind") == "market_evidence" or "market_profile" in value or "market_evidence" in value
               for value in documents_with_metadata(documents))


def normalized_relative(config: dict[str, Any], source_id: str) -> str:
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    directory = artifact_path(sources.get("normalized_dir", "sources/normalized"))
    value = source_id.lower().replace(":", "__colon__")
    value = re.sub(r"[/\s]+", "-", value)
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).replace("__colon__", "--").replace("-.", ".").strip("-")
    return f"{directory}/{value or 'source'}.md"


def read_workspace_file(root: Path, relative: str, *, legacy: bool = False) -> bytes:
    artifact_path(relative)
    path = root / relative
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024:
            raise EvidenceInvalid("unsafe_usage_workspace_file")
        if legacy and sys.platform == "win32":
            data = read_legacy_file(root, relative, observation(info), 16 * 1024 * 1024)
        else:
            data = read_observed_file(root, relative, observation(info))
        if observation(path.lstat()) != observation(info):
            raise EvidenceInvalid("usage_workspace_changed")
        return data
    except OSError as exc:
        raise EvidenceInvalid("usage_workspace_file_unavailable") from exc


def resolve_revision(view: UsageView, source_id: str, revision: str | None = None,
                     normalized: bytes | None = None) -> tuple[str, dict[str, Any]]:
    candidates = []
    for identifier, record in view.state.revisions.items():
        if record["source_id"] != source_id or revision is not None and identifier != revision:
            continue
        path = record["descriptor"]["normalized_path"]
        if normalized is not None and (path is None or record["files"][path] != normalized):
            continue
        candidates.append((identifier, record))
    if len(candidates) != 1:
        raise EvidenceInvalid("usage_revision_missing_or_ambiguous")
    return candidates[0]


def source_decision(root: Path, config: dict[str, Any], source_id: str,
                    documents: list[dict[str, Any]], *, uses: list[str] | None = None,
                    purpose: str = "research", view: UsageView | None = None,
                    normalized: bytes | None = None) -> dict[str, Any]:
    uses = uses or ["retrieval"]
    reasons, claimed, present = claims(documents, uses)
    if reasons:
        return {"eligible": False, "reasons": reasons}
    if not configured(config):
        allowed = not present and not requires_authority(documents) and "training" not in uses
        return {"eligible": allowed, "reasons": [] if allowed else ["usage_authority_required"],
                "compatibility": "legacy-research" if allowed else None}
    try:
        if view is None:
            with current_view(root, config) as locked:
                return source_decision(root, config, source_id, documents, uses=uses, purpose=purpose,
                                       view=locked, normalized=normalized)
        data = normalized if normalized is not None else read_workspace_file(root, normalized_relative(config, source_id))
        revision, _record = resolve_revision(view, source_id, claimed, data)
        return view.check(revision, uses=uses, purpose=purpose, consumer="evidence-wiki")
    except (EvidenceInvalid, OSError, ScriptRefusal) as exc:
        return {"eligible": False, "reasons": [str(exc) if isinstance(exc, EvidenceInvalid) else "usage_workspace_file_unavailable"]}


def normalized_issues(root: Path | None, config: dict[str, Any], record: dict[str, Any],
                      normalized: dict[str, Any]) -> list[str]:
    if root is None:
        reasons, _revision, present = claims([record, normalized], ["retrieval"])
        return reasons or (["usage_original_context_required"] if configured(config) or present or requires_authority([record, normalized]) else [])
    source_id = record.get("id") or normalized.get("source_id")
    if not isinstance(source_id, str):
        return ["usage_source_identity_required"] if configured(config) else []
    return source_decision(root, config, source_id, [record, normalized])["reasons"]


def require_host_intake(config: dict[str, Any], documents: list[dict[str, Any]] | None = None) -> None:
    reasons, _revision, present = claims(documents or [], ["retrieval"])
    if configured(config) or present or reasons:
        raise refusal("protected_capture_requires_host_sanitization")


def require_legacy_export(config: dict[str, Any]) -> None:
    if configured(config) and not captured_config(config):
        raise refusal("protected_publication_requires_approved_artifact_closure")


def bytes_have_claims(relative: str, data: bytes, config: dict[str, Any]) -> bool:
    """Recognize declarations in captured source bytes without making a temporary copy."""
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    manifest = sources.get("manifest_path", "sources/manifest.jsonl")
    normalized = sources.get("normalized_dir", "sources/normalized")
    if relative != manifest and not (relative.startswith(str(normalized).rstrip("/") + "/") and relative.endswith(".md")):
        return False
    markers = CLAIM_KEYS | {"market_evidence", "market_profile"}
    try:
        if relative == manifest:
            documents = []
            for line in data.splitlines():
                if line.strip():
                    try:
                        documents.append(json.loads(line))
                    except (ValueError, RecursionError):
                        if any(key.encode() in line for key in markers):
                            raise EvidenceInvalid("usage_declarations_unreadable") from None
        else:
            pieces = data.decode("utf-8").split("---", 2)
            documents = [safe_load(pieces[1])] if len(pieces) == 3 and not pieces[0] else []
        return claims(documents, ["retrieval", "export"])[2] or requires_authority(documents)
    except (ValueError, yaml.YAMLError, RecursionError) as exc:
        if any(key.encode() in data for key in markers):
            raise refusal("usage_declarations_unreadable") from exc
        # Existing structural validation still owns malformed legacy records.
        return False


def workspace_has_claims(root: Path, config: dict[str, Any]) -> bool:
    """Inspect bounded source declarations before allowing a legacy cache or export."""
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    manifest = sources.get("manifest_path", "sources/manifest.jsonl")
    normalized = sources.get("normalized_dir", "sources/normalized")
    try:
        artifact_path(manifest)
        artifact_path(normalized)
        paths = [manifest] if (root / manifest).exists() else []
        directory = root / normalized
        if directory.is_symlink() or (directory.exists() and
                                      getattr(directory.lstat(), "st_file_attributes", 0) & 0x400):
            raise EvidenceInvalid("unsafe_usage_workspace_file")
        # Bound discovery as well as file contents. Do not follow directory links.
        pending = [directory] if directory.exists() else []
        entries = 0
        while pending:
            for path in pending.pop().iterdir():
                entries += 1
                if entries > 8192:
                    raise EvidenceInvalid("usage_workspace_bound_exceeded")
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise EvidenceInvalid("unsafe_usage_workspace_file")
                if path.is_dir():
                    pending.append(path)
                elif path.suffix == ".md":
                    paths.append(path.relative_to(root).as_posix())
        total = 0
        for relative in sorted(paths):
            data = read_workspace_file(root, relative, legacy=True)
            total += len(data)
            if total > 64 * 1024 * 1024:
                raise EvidenceInvalid("usage_workspace_bound_exceeded")
            if bytes_have_claims(relative, data, config):
                return True
        return False
    except (OSError, ValueError, yaml.YAMLError, RecursionError, ScriptRefusal) as exc:
        raise refusal(str(exc) if isinstance(exc, EvidenceInvalid) else "usage_declarations_unreadable") from exc


def require_unrestricted_legacy(root: Path, config: dict[str, Any]) -> None:
    if captured_view(root, config) is not None:
        return
    require_legacy_export(config)
    if workspace_has_claims(root, config):
        raise refusal("explicit_usage_requires_host_authorization")


def original_artifacts(root: Path, config: dict[str, Any], record: dict[str, Any]) -> dict[str, bytes] | None:
    if not configured(config):
        return None
    reasons, revision, _present = claims([record], ["retrieval"])
    if reasons:
        raise EvidenceInvalid(reasons[0])
    with current_view(root, config) as view:
        normalized = None if revision is not None else read_workspace_file(root, normalized_relative(config, record["id"]))
        identifier, original = resolve_revision(view, record["id"], revision, normalized)
        verdict = view.check(identifier, uses=["retrieval"], purpose="research", consumer="evidence-wiki")
        if not verdict["eligible"]:
            raise EvidenceInvalid(verdict["reasons"][0])
        prefix = original["descriptor"]["evidence_root"]
        if prefix is None:
            raise EvidenceInvalid("usage_original_artifacts_missing")
        return {path[len(prefix) + 1:]: data for path, data in original["files"].items() if path.startswith(prefix + "/")}
