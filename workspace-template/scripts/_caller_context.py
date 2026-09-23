#!/usr/bin/env python3
"""Frozen caller-run controls over existing run, claim and publication owners."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

import yaml
from _evidence_revision import capture_workspace, content_id
from _record_artifacts import json_document
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
SCHEMA = "evidence-caller-context/v1"


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def builtin_integrations(config):
    """Read-only caller inspection never activates unqualified entry points."""
    registry = sibling("_provider_registry")
    integrations = config.get("integrations", {})
    if not isinstance(integrations, dict):
        return False
    for phase, supported in (
        ("discovery", registry.DISCOVERY_PROVIDER_IDS),
        ("acquisition", registry.ACQUISITION_PROVIDER_IDS),
    ):
        section = integrations.get(phase, {})
        if not isinstance(section, dict):
            return False
        selected = section.get("providers", [])
        if not isinstance(selected, list) or not all(
            isinstance(value, str) and value in supported for value in selected
        ):
            return False
    return True


def capture(root):
    if sibling("_delegation_gate").live_pending_orders(Path(root)):
        raise ValueError("caller_managed_protocol_required")
    revision = capture_workspace(Path(root).resolve())
    config = yaml.safe_load(revision.files["research.yml"])
    if not isinstance(config, dict) or not builtin_integrations(config):
        raise ValueError("caller_provider_requires_explicit_qualification")
    if "docs/research-requirements.json" in revision.files:
        document = json_document(revision.files["docs/research-requirements.json"])
        if (
            document.get("schema_version") != "evidence-research-requirements/v1"
            or document.get("request", {}).get("schema_version") not in {"1.0", "2.0"}
            or document["request"].get("kind") != "research_request"
        ):
            raise ValueError("caller_requirements_version_unsupported")
        request = document["request"]["payload"]
        if request["authority"]["role"] != "caller" or "local_research" not in request["authority"]["allowed_actions"]:
            raise ValueError("caller_research_authority_not_declared")
    policy = sibling("_strict_evidence").resolve_policy(root, config)
    selected = {
        "research.yml",
        "workspace-system.yml",
        "AGENTS.md",
        "skills/research-run.md",
        "docs/installed-agent.md",
        "docs/caller-research.md",
    }
    if policy:
        selected.update(policy["instructions"])
        for path, expected in policy["instructions"].items():
            if path not in revision.files or "sha256:" + hashlib.sha256(revision.files[path]).hexdigest() != expected:
                raise ValueError("caller_instruction_changed")
    expected_scripts = {"scripts/" + path.name for path in SCRIPT_DIR.glob("*.py")}
    actual_scripts = {path for path in revision.files if path.startswith("scripts/")}
    if actual_scripts != expected_scripts:
        raise ValueError("caller_workspace_scripts_not_qualified")
    for script in SCRIPT_DIR.glob("*.py"):
        relative = "scripts/" + script.name
        if revision.files.get(relative) != script.read_bytes():
            raise ValueError("caller_workspace_scripts_not_qualified")
    selected.update(
        path
        for path in revision.files
        if path.startswith(("domain-packs/", "scripts/")) or path == "docs/research-requirements.json"
    )
    required = selected - {"docs/installed-agent.md"}
    if not required <= set(revision.files):
        raise ValueError("caller_controls_missing")
    executable = Path(sys.executable).resolve()
    if executable.stat().st_size > 67_108_864:
        raise ValueError("caller_interpreter_bound")
    environment = {
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
        "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "invocation_id": content_id(
            "caller-interpreter",
            [str(Path(sys.executable).parent.resolve() / Path(sys.executable).name), str(Path(sys.prefix).resolve())],
        ),
        "dependencies": {
            name: importlib.metadata.version(name) for name in ("PyYAML", "pypdf", "ruamel.yaml", "tzdata")
        },
    }
    value = {
        "schema_version": SCHEMA,
        "environment": environment,
        "files": {
            path: hashlib.sha256(revision.files[path]).hexdigest()
            for path in sorted(selected)
            if path in revision.files
        },
        "policy_id": sibling("_strict_contract").artifact_id(policy) if policy else None,
        "producer_id": sibling("_selected_publication").producer_identity(),
    }
    return {**value, "context_id": content_id(SCHEMA, value)}


def validate(root, document, *, agent_id=None, transfer=False):
    expected = document.get("caller_context")
    if expected is None:
        return
    if expected != capture(root):
        raise ValueError("caller_controls_changed")
    if agent_id is not None and not transfer and document.get("agent_id") != agent_id:
        raise ValueError("caller_run_owned_by_another_agent")


def original_questions(root, config):
    """Account for every frozen original and derived question without rewriting IDs."""
    path = Path(root) / "docs/research-requirements.json"
    status = sibling("question_status")
    questions = status.collect_questions(status.questions_directory(root, config))
    if len(questions) > 300:
        raise ValueError("caller_question_bound")
    if not path.exists():
        return {
            "basis": "current_workspace_questions",
            "originals": [
                {"id": row["slug"], "text": row["question"], "question_ids": [row["slug"]]} for row in questions
            ],
            "expected": {row["slug"]: row["slug"] for row in questions},
            "gaps": ["original_request_not_frozen"],
        }
    raw = path.read_bytes()
    if len(raw) > 1_048_576:
        raise ValueError("caller_requirements_bound")
    requirements = json_document(raw)
    if (
        requirements.get("schema_version") != "evidence-research-requirements/v1"
        or requirements.get("request", {}).get("schema_version") not in {"1.0", "2.0"}
        or requirements["request"].get("kind") != "research_request"
    ):
        raise ValueError("caller_requirements_version_unsupported")
    payload = requirements["request"]["payload"]
    original, derived = payload["questions"], payload["derived_questions"]
    if len(original) + len(derived) > 300 or not original:
        raise ValueError("caller_question_bound")
    ids = [row["id"] for row in [*original, *derived]]
    originals_set = {row["id"] for row in original}
    if len(ids) != len(set(ids)) or any(
        not row["original_ids"] or not set(row["original_ids"]) <= originals_set for row in derived
    ):
        raise ValueError("caller_original_mapping_invalid")
    expected, gaps = {}, []
    for row in questions:
        frontmatter = status.load_frontmatter(status.questions_directory(root, config) / (row["slug"] + ".md"))
        metadata = frontmatter.get("metadata", {})
        qid = metadata.get("research_id")
        if qid in expected:
            raise ValueError("caller_question_mapping_ambiguous")
        if qid:
            expected[qid] = row["slug"]
        definition = next((item for item in [*original, *derived] if item["id"] == qid), None)
        if (
            definition is None
            or metadata.get("original_text") != definition["text"]
            or row["question"] != definition["text"].strip()
            or metadata.get("original_ids_json")
            != json.dumps(definition.get("original_ids", [definition["id"]]), ensure_ascii=False)
        ):
            gaps.append("question_mapping_or_text_changed:" + row["slug"])
    for row in [*original, *derived]:
        if row["id"] not in expected:
            gaps.append("original_or_derived_question_missing:" + row["id"])
    originals = [
        {
            "id": row["id"],
            "text": row["text"],
            "question_ids": [row["id"], *[item["id"] for item in derived if row["id"] in item["original_ids"]]],
        }
        for row in original
    ]
    return {"basis": "frozen_research_request", "originals": originals, "expected": expected, "gaps": sorted(set(gaps))}


def completion_readiness(root):
    """Caller-run completion is derived from current release and original accounting."""
    core = sibling("_strict_evidence")
    config = core.configuration(root)
    policy = core.resolve_policy(root, config)
    if policy is None:
        return {"verdict": "no_ship", "findings": [{"code": "caller_strict_release_required"}]}
    publication = core.publication(root)
    mapping = original_questions(root, config)
    accepted = {row["slug"] for row in publication["questions"] if row["accepted"]}
    wanted = {mapping["expected"].get(qid) for row in mapping["originals"] for qid in row["question_ids"]}
    ready = bool(wanted) and not mapping["gaps"] and wanted <= accepted
    return {
        "verdict": "ship" if ready else "no_ship",
        "generated_at": publication["evaluated_at"],
        "findings": [] if ready else [{"code": "caller_original_questions_unresolved"}],
        "basis_id": publication["basis_id"],
        "original_outcomes": publication["original_outcomes"],
    }
