#!/usr/bin/env python3
"""Qualify all source bytes before an assessment materializes a workspace."""

from __future__ import annotations

from pathlib import PurePosixPath

import yaml
from _evidence_authority import EvidenceInvalid, bounded_list, name
from _record_artifacts import artifact_path, json_document
from _snapshot_verifier import ClaimLoader
from _usage_gate import claims, normalized_relative, resolve_revision
from _yaml_safe import split_frontmatter_block


def require(condition, reason):
    if not condition:
        raise EvidenceInvalid(reason)


def qualify_raw_files(files, source, original):
    """Match declared captures and provenance against complete approved file closures."""
    prefix = original["descriptor"]["evidence_root"]
    require(prefix is not None, "assessment_approved_raw_closure_missing")
    approved = {path[len(prefix) + 1:]: data for path, data in original["files"].items()
                if path.startswith(prefix + "/")}
    available = files.keys() | approved.keys()
    selected = set()
    for raw in bounded_list(source.get("raw_paths"), minimum=1):
        raw = artifact_path(raw)
        members = {raw} if raw in available else {path for path in available if path.startswith(raw + "/")}
        require(bool(members), "assessment_raw_revision_mismatch")
        selected.update(members)
        sidecar = raw + ".provenance.yml"
        if sidecar in available:
            selected.add(sidecar)
    provenance = [source.get("provenance"), *bounded_list(source.get("additional_provenance", []))]
    for entry in provenance:
        if isinstance(entry, dict) and "sidecar_path" in entry:
            selected.add(artifact_path(entry["sidecar_path"]))
    for path in selected:
        require(path in files and path in approved and files[path] == approved[path], "assessment_raw_revision_mismatch")
    return {path: files[path] for path in selected}


def qualify_capture(files, config, view, *, purpose, consumer):
    """Read bounded captured bytes, then approve exact host-owned closures."""
    sources = config.get("sources", {})
    manifest = artifact_path(sources.get("manifest_path", "sources/manifest.jsonl"))
    normalized_dir = artifact_path(sources.get("normalized_dir", "sources/normalized"))
    raw_roots = ["raw", *config.get("raw", {}).get("source_roots", [])]
    raw_roots = [artifact_path(value).rstrip("/") for value in bounded_list(raw_roots)]
    require(manifest in files, "assessment_source_manifest_missing")
    records = []
    for line in files[manifest].splitlines():
        if line.strip():
            records.append(json_document(line))
            require(len(records) <= 64, "assessment_source_bound_exceeded")
    require(bool(records), "assessment_required_inputs_missing")
    approved_raw, approved_normalized, selected, ancestry = {}, set(), {}, set()
    for source in records:
        require(isinstance(source, dict), "assessment_source_manifest_invalid")
        source_id = name(source.get("id"))
        require(source_id not in selected, "assessment_source_identity_duplicate")
        relative = normalized_relative(config, source_id)
        require(relative in files, "assessment_normalized_input_missing")
        block, _body = split_frontmatter_block(files[relative].decode("utf-8"))
        require(block is not None, "assessment_normalized_metadata_missing")
        metadata = yaml.load(block, Loader=ClaimLoader)  # noqa: S506 -- restricted SafeLoader subclass
        require(isinstance(metadata, dict) and metadata.get("source_id") == source_id,
                "assessment_normalized_source_mismatch")
        reasons, claimed, _present = claims([source, metadata], ["retrieval", "export"])
        require(not reasons, reasons[0] if reasons else "assessment_local_use_denied")
        revision, original = resolve_revision(view, source_id, claimed, files[relative])
        for approved_purpose, approved_consumer in {("research", "evidence-wiki"), (purpose, consumer)}:
            decision = view.check(revision, uses=["retrieval", "export"], purpose=approved_purpose, consumer=approved_consumer)
            require(decision["eligible"] and decision["complete"],
                    decision["reasons"][0] if decision["reasons"] else "assessment_use_incomplete")
            ancestry.update(decision["ancestors"])
        approved_raw.update(qualify_raw_files(files, source, original))
        approved_normalized.add(relative)
        selected[source_id] = revision
    for relative in files:
        if relative.startswith(normalized_dir + "/") and PurePosixPath(relative).suffix == ".md":
            require(relative in approved_normalized, "assessment_unapproved_normalized_input")
        if any(relative == root or relative.startswith(root + "/") for root in raw_roots):
            require(relative in approved_raw or PurePosixPath(relative).name == ".gitkeep" and not files[relative].strip(),
                    "assessment_unapproved_raw_input")
    ancestry.update(selected.values())
    require(len(ancestry) <= 128 and all(revision in view.state.revisions for revision in ancestry),
            "assessment_source_ancestry_incomplete")
    return {"selected": dict(sorted(selected.items())), "ancestors": sorted(ancestry),
            "coverage": "all_workspace_sources", "complete": True}
