"""Canonical question refresh rendering; no review or authority is created."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ._pack_io import json_document
from .host_context import _require_mutation_workspace_gate, current_context
from .mutation_contracts import canonical, digest
from .pack_discovery import owner
from .research_session_contracts import refuse

MARKER = "docs/research-refresh.json"
MARKER_SCHEMA = "research-refresh-marker/v1"
PLAN_SCHEMA = "research-refresh-owner-plan/v1"


def sha(raw):
    return None if raw is None else "sha256:" + hashlib.sha256(raw).hexdigest()


def _host(root):
    context = current_context()
    if context is None:
        refuse("HOST_CAPABILITY_UNAVAILABLE")
    context.require_grant("refresh_owner.apply")
    root = context.require_path(Path(root), write=True).resolve(strict=True)
    _require_mutation_workspace_gate(root)
    return root, context


def _material(files, excluded=()):
    return {name: sha(raw) for name, raw in files.items() if name not in {*excluded, "log.md"}
        and not name.startswith(("runs/", ".locks/", "raw/.locks/"))}


def _check_active_runs(capture, arguments):
    controller = owner("run_controller")
    originals = set(arguments["original_ids"])
    for path, raw in capture.files.items():
        parts = path.split("/")
        if len(parts) != 3 or parts[0] != "runs" or parts[2] != controller.RUN_STATE_FILENAME:
            continue
        if parts[1] == arguments["run_id"]:
            continue
        document = json_document(raw)
        binding = document.get("research_session") or {}
        if (document["state"]["current"] not in controller.TERMINAL_STATES
                and originals.intersection(binding.get("original_ids", []))):
            refuse("ACTIVE_ORIGINAL_RUN")


def prepare(root, arguments, *, input_sha256):
    root, context = _host(root)
    revision = owner("_evidence_revision")
    capture = revision.capture_workspace(root)
    basis = revision.content_id("research-session-input-basis/v1", _material(capture.files))
    if basis != arguments["expected_basis_id"]:
        refuse("BASIS_CHANGED")
    prior_marker = capture.files.get(MARKER)
    if sha(prior_marker) != arguments["expected_marker_sha256"]:
        refuse("REFRESH_HEAD_CHANGED")
    if prior_marker is not None and json_document(prior_marker).get("schema_version") != MARKER_SCHEMA:
        refuse("REFRESH_HEAD_CHANGED")
    config = owner("_strict_evidence").configuration(root)
    controller = owner("run_controller")
    document = controller._session_state(root, arguments["run_id"])
    binding = document.get("research_session") or {}
    parent = controller._session_state(root, arguments["previous_run_id"])
    if (parent["state"]["current"] not in controller.TERMINAL_STATES
            or (parent.get("research_session") or {}).get("original_ids") != arguments["original_ids"]
            or arguments["previous_run_id"] == arguments["run_id"]):
        refuse("REFRESH_TERMINAL_PARENT_REQUIRED")
    _check_active_runs(capture, arguments)
    if (document["state"]["current"] in controller.TERMINAL_STATES
            or binding.get("actor_ref") != context.actor_ref
            or binding.get("original_ids") != arguments["original_ids"]):
        refuse("BINDING_CHANGED")
    mapping = owner("_caller_context").original_questions(root, config)
    if mapping["gaps"] or [row["id"] for row in mapping["originals"]] != arguments["original_ids"]:
        refuse("ORIGINALS_CHANGED")
    marker = {"schema_version": MARKER_SCHEMA, "run_id": arguments["run_id"],
        "previous_run_id": arguments["previous_run_id"], "request_id": arguments["request_id"],
        "parent_marker_sha256": sha(prior_marker), "before_basis_id": basis,
        "binding_id": digest(binding), "original_ids": arguments["original_ids"],
        "current_acceptance": "not_evaluated"}
    marker["refresh_id"] = digest(marker)
    outputs = [{"path": MARKER, "data": canonical(marker) + b"\n", "prior_sha256": sha(prior_marker)}]
    qstatus, claims, resolver = owner("question_status"), owner("question_claim"), owner("question_resolve")
    directory = qstatus.questions_directory(root, config)
    questions = qstatus.collect_questions(directory)
    slugs = {slug for row in mapping["originals"] for slug in row["question_ids"]}
    for question in questions:
        if question["slug"] not in slugs:
            continue
        path = directory / question["path"]
        context.require_path(path, write=True)
        relative = path.relative_to(root).as_posix()
        raw = capture.files[relative]
        parts = claims.split_frontmatter_lines(raw.decode("utf-8"))
        front = claims.frontmatter_mapping(parts[0])
        if front.get("claimed_by") not in {None, context.actor_ref}:
            refuse("FOREIGN_QUESTION_CLAIM")
        if front.get("type") != "question":
            refuse("ORIGINALS_CHANGED")
        text = resolver.apply_resolution_edits(raw.decode("utf-8"),
            {"status": "open", "coverage_required": True},
            ("claimed_by", "claimed_at", "answer_page", "answered_by", "answered_at", "grounding",
             "approved_by", "approved_at", "human_review_approved", "human_reviews", "human_review_requested_at",
             "human_review_status", "human_review_required", "human_review_policies", "blocked_reason",
             "blocking_request_ids", "confidence", "evidence_strength", "resolution_reason",
             *resolver.GROUNDING_VERIFICATION_STAMPS))
        outputs.append({"path": relative, "data": text.encode("utf-8"), "prior_sha256": sha(raw)})
    if len(outputs) != len(slugs) + 1:
        refuse("ORIGINALS_CHANGED")
    context.require_path(root / MARKER, write=True)
    return {"schema_version": PLAN_SCHEMA, "input_sha256": input_sha256, "arguments": arguments,
        "dependencies_sha256": revision.content_id("research-refresh-dependencies/v1", _material(capture.files, [row["path"] for row in outputs])),
        "marker": marker, "outputs": outputs, "report": {"schema_version": "research-refresh-prepared/v1",
            "refresh_id": marker["refresh_id"], "marker_sha256": sha(outputs[0]["data"]),
            "question_slugs": sorted(slugs), "accepted": False, "review_registered": False}}


def verify(root, plan):
    root, context = _host(root)
    capture = owner("_evidence_revision").capture_workspace(root)
    _check_active_runs(capture, plan["arguments"])
    paths = [row["path"] for row in plan["outputs"]]
    if owner("_evidence_revision").content_id("research-refresh-dependencies/v1", _material(capture.files, paths)) != plan["dependencies_sha256"]:
        refuse("BASIS_CHANGED")
    for row in plan["outputs"]:
        context.require_path(root / row["path"], write=True)
        owner("_owner_paths").checked_path(root, row["path"], field="refresh_output", kind="file")
        if sha(capture.files.get(row["path"])) not in {row["prior_sha256"], sha(row["data"])}:
            refuse("REFRESH_OUTPUT_CHANGED")
    return capture


def apply(root, *, plan):
    root, _context = _host(root)
    capture = verify(root, plan)
    writer = owner("question_claim")
    for row in plan["outputs"]:
        if capture.files.get(row["path"]) != row["data"]:
            verify(root, plan)
            writer.write_page_atomic(root / row["path"], row["data"].decode("utf-8"))
    capture = verify(root, plan)
    if any(capture.files.get(row["path"]) != row["data"] for row in plan["outputs"]):
        refuse("REFRESH_OUTPUT_CHANGED")
    return plan["report"]
