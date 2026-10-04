#!/usr/bin/env python3
"""One controlled unaccepted claims inventory publication over captured inputs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import MappingProxyType

import yaml
from _draft_admission_contract import MAX_BYTES, RESULT, proposal_document, validate_document
from _evidence_revision import canonical_bytes, capture_workspace, content_id
from _native_fs import os
from _owner_paths import checked_path
from _record_artifacts import artifact_path, json_document
from _script_errors import ScriptRefusal, is_refusal
from _workspace_module_loader import load_workspace_module

SCRIPT_DIR = Path(__file__).resolve().parent
_SIBLINGS = {}
PLAN = "evidence-draft-effect-plan/v1"
LOCK = ".locks/draft-admission.lock"
REQUIREMENTS = "docs/research-requirements.json"


def sibling(name):
    return load_workspace_module(SCRIPT_DIR, name, cache=_SIBLINGS)


def refuse(reason, *, unsupported=False):
    raise ScriptRefusal("DRAFT_ADMISSION_UNSUPPORTED" if unsupported else "DRAFT_ADMISSION_REFUSED",
        "The draft inventory cannot be admitted for this request.", exit_code=2, recoverable=False,
        details={"reason": reason}, remediation="Retain the last valid inventory; supply current exact original/source scope and use the serialized admission route.") from None


def require(condition, reason, *, unsupported=False):
    if not condition:
        refuse(reason, unsupported=unsupported)


def typed(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError, yaml.YAMLError) as exc:
            if is_refusal(exc):
                raise
            refuse("admission_input_invalid_or_unreadable")
    return call


def _host(root):
    bridge = sys.modules.get("evidence_wiki.host_context")
    context = bridge.current_context() if bridge is not None else None
    require(context is not None and context.mutation_control_directory is not None, "serialized_admission_required")
    context.require_grant("strict_evidence.run_admit_document")
    root = context.require_path(Path(root).expanduser().absolute(), write=True)
    bridge._require_mutation_workspace_gate(root)
    return root, context


def clock(value):
    require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None, "clock_invalid")
    return value.astimezone(timezone.utc)


def current_time():
    return datetime.now(timezone.utc)


def file_hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def configuration(capture):
    require("research.yml" in capture.files and len(capture.files["research.yml"]) <= MAX_BYTES, "configuration_missing_or_large")
    value = yaml.safe_load(capture.files["research.yml"])
    require(type(value) is dict, "configuration_invalid")
    return value


def target(root, config, policy):
    relative = artifact_path(policy["claims_path"])
    path = checked_path(root, relative, field="claims_inventory", kind="file")
    context = sys.modules["evidence_wiki.host_context"].current_context()
    context.require_path(path, write=True)
    protected = {"research.yml", "workspace-system.yml", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "index.md", "log.md", REQUIREMENTS,
                 *policy["instructions"]}
    sources = config.get("sources") if type(config.get("sources")) is dict else {}
    protected.update(artifact_path(sources.get(key, default)) for key, default in (
        ("manifest_path", "sources/manifest.jsonl"), ("source_requests_path", "sources/source-requests.jsonl")))
    protected.add(artifact_path(sources.get("jurisdictions_path", "sources/discovery/jurisdictions.yml")))
    protected.add(artifact_path(sibling("_evidence_policies").jurisdictions_path_text(config)))
    trees = ["raw", "scripts", "skills", "runs", ".locks", ".evidence-wiki", "domain-packs",
        artifact_path(sources.get("normalized_dir", "sources/normalized")),
        artifact_path(sources.get("coverage_dir", "sources/coverage")), "sources/discovery"]
    raw = config.get("raw") if type(config.get("raw")) is dict else {}
    roots = raw.get("source_roots", [])
    require(type(roots) is list and len(roots) <= 32, "raw_scope_invalid")
    trees.extend(artifact_path(value) for value in roots)
    question_dir = sibling("question_status").questions_directory(root, config)
    question_dir = checked_path(root, question_dir, field="question_scope", kind="directory").relative_to(root).as_posix()
    trees.append(question_dir)
    require(relative not in protected and not any(relative == prefix or relative.startswith(prefix + "/") for prefix in trees), "claims_target_overlaps_original_or_control")
    require(Path(relative).suffix.lower() == ".json", "claims_target_requires_json")
    return relative


def originals(capture, root, config, policy, proposal):
    require(REQUIREMENTS in capture.files and REQUIREMENTS in policy["instructions"], "original_context_missing")
    frozen = sibling("_requirements_bridge").effective(root, capture.files[REQUIREMENTS])
    require(frozen.get("schema_version") == "evidence-research-requirements/v1", "original_context_invalid")
    request = frozen.get("request")
    require(type(request) is dict and request.get("schema_version") in {"1.0", "2.0"}
            and request.get("kind") == "research_request", "original_request_version_unsupported", unsupported=True)
    from evidence_wiki.onboarding_contract import decode_document

    version = "v2" if request["schema_version"] == "2.0" else "v1"
    try:
        decoded = decode_document("onboarding/research_request/" + version, canonical_bytes(request))
    except (Exception, SystemExit):
        refuse("original_request_contract_invalid")
    payload = decoded["payload"]
    selected = payload["target"]
    writable = Path(selected["writable_root"])
    relative = Path(selected["relative_path"])
    require(writable.is_absolute() and ".." not in writable.parts and not relative.is_absolute()
            and ".." not in relative.parts, "original_target_unsupported", unsupported=True)
    declared_root = writable / relative
    require(declared_root.resolve(strict=True) == root, "original_target_changed")
    authority = payload["authority"]
    writable_roots = authority["writable_roots"]
    require(all(Path(value).is_absolute() and ".." not in Path(value).parts for value in writable_roots),
            "original_writable_scope_unsupported", unsupported=True)
    require(any(declared_root.is_relative_to(Path(value)) and root.is_relative_to(Path(value).resolve(strict=True))
                for value in writable_roots), "original_writable_scope_changed")
    scopes = []
    has_uri = False
    for value in authority["source_scope"]:
        if type(value) is str and "://" in value:
            from urllib.parse import urlsplit
            try:
                declared=urlsplit(value)
                _port=declared.port
                require(declared.scheme=="https" and declared.hostname is not None
                    and declared.username is None and declared.password is None
                    and not declared.query and not declared.fragment, "original_source_scope_unsupported", unsupported=True)
            except ValueError:
                refuse("original_source_scope_unsupported", unsupported=True)
            has_uri = True
            continue
        path = Path(value)
        require(path.is_absolute() and ".." not in path.parts and path.is_relative_to(declared_root),
                "original_source_scope_unsupported", unsupported=True)
        path = checked_path(declared_root, path, field="original_source_scope")
        require(path.exists(), "original_source_scope_missing")
        scopes.append((path, path.is_dir()))
    require(scopes, "original_source_scope_unsupported" if has_uri else "original_source_scope_missing", unsupported=has_uri)
    for source in payload["sources"]:
        require(source["kind"] == "local_file", "original_source_locator_unsupported", unsupported=True)
        path = Path(source["locator"])
        require(path.is_absolute() and ".." not in path.parts and path.is_relative_to(declared_root),
                "original_source_locator_unsupported", unsupported=True)
        path = checked_path(declared_root, path, field="original_source_locator", kind="file")
        require(any(path == allowed or directory and path.is_relative_to(allowed) for allowed, directory in scopes),
                "original_source_locator_outside_scope")
    primary, derived = payload.get("questions"), payload.get("derived_questions")
    require(type(primary) is list and 1 <= len(primary) <= 100 and type(derived) is list and len(primary) + len(derived) <= 100, "original_context_invalid")
    rows, original = {}, {}
    for item in primary + derived:
        require(type(item) is dict and type(item.get("id")) is str and type(item.get("text")) is str
                and item["id"] not in rows, "original_context_invalid")
        rows[item["id"]] = item
    for item in primary:
        original[item["id"]] = item["text"]
    for item in derived:
        ids = item.get("original_ids")
        require(type(ids) is list and len(ids) == 1, "multi_origin_mapping_unsupported", unsupported=True)
        require(ids[0] in original, "original_context_invalid")
    qstatus, verifier = sibling("question_status"), sibling("verify_quotes")
    directory = qstatus.questions_directory(root, config).relative_to(root).as_posix()
    pages, seen, mapping = {}, set(), {identifier: [] for identifier in original}
    for name, raw in capture.files.items():
        if Path(name).parent.as_posix() != directory or not name.endswith(".md"):
            continue
        frontmatter, _body = verifier.split_page(raw.decode("utf-8"))
        if frontmatter.get("type") != "question":
            continue
        metadata = frontmatter.get("metadata")
        require(type(metadata) is dict and type(metadata.get("research_id")) is str, "original_lineage_missing")
        identifier = metadata["research_id"]
        require(identifier in rows and identifier not in seen, "original_lineage_changed")
        seen.add(identifier)
        item = rows[identifier]
        encoded = metadata.get("original_ids_json")
        require(type(encoded) is str and len(encoded.encode()) <= 4096, "original_lineage_invalid")
        ids = json.loads(encoded)
        require(type(ids) is list and len(ids) == 1, "multi_origin_mapping_unsupported", unsupported=True)
        expected_ids = item.get("original_ids", [identifier])
        require(ids == expected_ids and ids[0] in original and metadata.get("original_text") == item["text"], "original_lineage_changed")
        actual = frontmatter.get("question") or frontmatter.get("summary")
        require(actual == item["text"].strip(), "original_question_changed")
        slug = Path(name).stem
        pages[slug] = {"slug": slug, "original_id": ids[0], "question": actual}
        mapping[ids[0]].append(slug)
    require(seen == set(rows), "original_question_accounting_incomplete")
    proposed = {item["slug"]: item for item in proposal["inventory"]["questions"]}
    require(proposed == pages, "question_accounting_changed")
    expected = {identifier: {"original_id": identifier, "text": text, "question_slugs": sorted(mapping[identifier])}
                for identifier, text in original.items()}
    actual = {item["original_id"]: {**item, "question_slugs": sorted(item["question_slugs"])} for item in proposal["originals"]}
    require(actual == expected and all(mapping.values()), "original_accounting_changed")
    mapped, captured_inputs = sibling("coverage_manifest").recovery_materialized_inputs(root, config, capture)
    # The live owner authenticated the revision; the disposable mapping view
    # carries that effective projection without acquiring clone-local custody.
    captured_inputs = type(captured_inputs)(MappingProxyType({**captured_inputs.files,
        REQUIREMENTS: canonical_bytes(frozen)}), captured_inputs.directories)
    with captured_inputs.materialize() as captured:
        accounting = sibling("_caller_context").original_questions(captured, mapped)
        require(accounting["basis"] == "frozen_research_request" and not accounting["gaps"], "original_lineage_changed")
    return scopes


def private_view(view):
    return None if view is None else {"state_id": view.state.state_id, "checkpoint": view.state.checkpoint,
                                     "authority_sha256": view.trust["content_hash"]}


def original_source_paths(root, capture, record, scopes, fingerprint_paths):
    values = record.get("raw_paths", [])
    require(type(values) is list and len(values) <= 256, "source_original_paths_invalid")
    values = list(values)
    values.extend(record[key] for key in ("latex_root", "raw_pdf") if record.get(key) is not None)
    values.extend(artifact_path(value) + ".provenance.yml" for value in list(values)
                  if artifact_path(value) + ".provenance.yml" in capture.files)
    values.extend(fingerprint_paths)
    values.extend(sibling("source_inventory").record_companion_paths(record))
    provenance = [record.get("provenance"), *record.get("additional_provenance", [])]
    for entry in provenance:
        if type(entry) is dict and entry.get("sidecar_path") is not None:
            values.append(entry["sidecar_path"])
    if record.get("entrypoint") is not None:
        require(type(record.get("latex_root")) is str, "source_original_paths_invalid")
        values.append(artifact_path(record["latex_root"]) + "/" + artifact_path(record["entrypoint"]))
    require(len(values) <= 10000, "source_original_paths_invalid")
    for value in set(values):
        relative = artifact_path(value)
        path = checked_path(root, relative, field="source_original")
        require(any(path == allowed or directory and path.is_relative_to(allowed) for allowed, directory in scopes),
                "source_outside_original_scope")
        require(relative in capture.files or any(name.startswith(relative + "/") for name in capture.files), "source_original_missing")


def _check_evidence(root, config, policy, capture, observed_at, scopes, records, captured, mapped, frozen, normalized_relative, claim, evidence):
    normalized, verifier, inventory = sibling("_normalized_contract"), sibling("verify_quotes"), sibling("source_inventory")
    identifier = evidence["source_id"]
    require(identifier in records, "source_not_inventoried")
    record = records[identifier]
    fingerprint_paths = [path.relative_to(captured).as_posix()
                         for path in inventory.raw_fingerprint_paths(captured, record)]
    original_source_paths(root, capture, record, scopes, fingerprint_paths)
    path, _label = verifier.normalized_record_path(captured, mapped, identifier)
    relative = path.relative_to(captured).as_posix()
    require(relative in capture.files and file_hash(capture.files[relative]) == evidence["record_sha256"], "source_record_revision_changed")
    metadata, body = verifier.normalized_record_content(path)
    require(metadata.get("source_id") == identifier, "source_identity_changed")
    problems = normalized.validate_document(path, metadata, body, manifest_by_id=records,
        normalized_root=captured / normalized_relative, project_root=captured, config=mapped)
    require(not problems, "source_record_contract_invalid")
    require(metadata.get("evidence_usable") is not False, "source_not_usable")
    provenance = metadata.get("provenance") or {}
    original_provenance = record.get("provenance") or {}
    require(type(provenance) is dict and type(original_provenance) is dict, "source_time_changed")
    parse_time = sibling("_evidence_authority").timestamp
    retrieved = parse_time(original_provenance.get("retrieved_at"))
    require(retrieved == parse_time(provenance.get("retrieved_at")) == parse_time(evidence["observed_at"]), "source_time_changed")
    age = (observed_at - retrieved).total_seconds()
    require(0 <= age <= policy["max_source_age_seconds"], "source_age_invalid")
    fingerprint = inventory.compute_raw_fingerprint(captured, record)
    require(fingerprint is None or fingerprint == record.get("raw_fingerprint") == metadata.get("raw_fingerprint"), "source_raw_revision_changed")
    profile = metadata.get("host_capture")
    if type(profile) is dict:
        expected_kind = {"primary": "primary", "excerpt": "excerpt", "generated_summary": "summary", "search_snippet": "unknown"}.get(profile.get("content_kind"))
        require(evidence["capture"] == expected_kind, "source_capture_kind_changed")
    decision = sibling("_usage_gate").source_decision(captured, mapped, identifier, [record, metadata], view=frozen,
        normalized=capture.files[relative], uses=["retrieval"], purpose="research")
    require(decision.get("eligible") is True, "source_use_not_permitted")
    entry = sibling("_strict_evidence").grounding_entry(claim, evidence)
    parsed = verifier.grounding_entries({"grounding": [entry]}, claim["question_slug"])
    require(len(parsed) == 1, "mechanical_grounding_unmatched")
    mechanical = verifier.verify_entry(captured, mapped, parsed[0])
    return {"result":"verified" if mechanical["result"] == "verified" else "failed",
        "reason":None if mechanical["result"] == "verified" else "mechanical_grounding_unmatched",
        "record_sha256":evidence["record_sha256"], "usage":decision, "mechanical":mechanical}


def iter_source_diagnostics(root, config, policy, capture, proposal, observed_at, scopes, *, _live_view=None, fail_fast=False):
    usage, publication = sibling("_evidence_usage"), sibling("_publication_context")
    manifest_relative, normalized_relative = sibling("normalize_sources").source_paths(config)
    checked_path(root, manifest_relative, field="source_manifest", kind="file")
    checked_path(root, normalized_relative, field="normalized_sources", kind="directory")
    records = {}
    raw_manifest = capture.files.get(manifest_relative, b"")
    require(len(raw_manifest) <= MAX_BYTES, "source_manifest_bound")
    for line in raw_manifest.splitlines():
        if not line.strip():
            continue
        record = json_document(line)
        identifier = record.get("id")
        require(type(identifier) is str and identifier not in records and len(records) < 1000, "source_manifest_invalid")
        records[identifier] = record
    diagnostics = []
    selected = nullcontext(_live_view) if _live_view is not None else usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with selected as view:
        binding = private_view(view)
        if _live_view is not None:
            require(observed_at >= view.now, "source_clock_regressed")
            view.now = observed_at
        frozen = view if _live_view is not None else usage.UsageView(view.state, view.trust, observed_at) if view is not None else None
        mapped, captured_inputs = sibling("coverage_manifest").recovery_materialized_inputs(root, config, capture)
        with captured_inputs.materialize() as captured, publication.authorized_capture(captured, mapped, frozen):
            for claim in proposal["inventory"]["claims"]:
                for index, evidence in enumerate(claim["evidence"]):
                    row = {"claim_id":claim["id"], "evidence_index":index, "source_id":evidence["source_id"],
                        "record_sha256":evidence["record_sha256"], "location_hint":evidence.get("location_hint"),
                        "anchor_pointer":evidence["anchor"]["pointer"] if evidence.get("anchor") is not None else None,
                        "form":"anchor" if evidence.get("anchor") is not None else "quote",
                        "result":"unknown", "reason":None, "mechanical":None,
                        "semantic_approval":"not_evaluated", "accepted":False}
                    try:
                        checked = _check_evidence(root, config, policy, capture, observed_at, scopes, records,
                            captured, mapped, frozen, normalized_relative, claim, evidence)
                        if fail_fast:
                            require(checked["result"] == "verified", "mechanical_grounding_unmatched")
                        row.update(checked)
                    except ScriptRefusal as error:
                        if fail_fast:
                            raise
                        reason = error.details.get("reason") if type(error.details) is dict else None
                        row.update(result="failed",reason=reason if type(reason) is str else "source_check_refused")
                    diagnostics.append(row)
        require(private_view(view) == binding, "source_authority_changed")
        if view is not None:
            view.revalidate(root, config)
        return diagnostics, binding


def source_checks(root, config, policy, capture, proposal, observed_at, scopes, *, _live_view=None):
    rows, binding = iter_source_diagnostics(root, config, policy, capture, proposal, observed_at, scopes,
        _live_view=_live_view, fail_fast=True)
    return len(rows), binding


def dependencies(capture, target_path, new_directories, controls, *, temporary=None):
    for name in controls["files"]:
        require(name not in capture.files or capture.files[name] in {b"", b"\0"}, "claims_control_unowned")
    files = {name: file_hash(raw) for name, raw in capture.files.items()
             if name != target_path and name != temporary and name not in controls["files"]}
    directories = [name for name in capture.directories if name not in new_directories and name not in controls["new_directories"]]
    return content_id("evidence-draft-inputs/v1", {"files": files, "directories": directories})


@typed
def prepare_admission(root, proposal, *, observed_at):
    root, _context = _host(root)
    proposal = proposal_document(proposal)
    observed_at = clock(observed_at)
    capture = capture_workspace(root)
    config = configuration(capture)
    policy = sibling("_strict_evidence").resolve_policy(root, config)
    require(policy is not None, "strict_policy_missing")
    sibling("_draft_computation").check(root, config, proposal)
    relative = target(root, config, policy)
    for path, expected in policy["instructions"].items():
        require(path in capture.files and sibling("_requirements_bridge").instruction_matches(root, path, capture.files[path], expected), "instructions_changed")
    scopes = originals(capture, root, config, policy, proposal)
    count, authority = source_checks(root, config, policy, capture, proposal, observed_at, scopes)
    raw = canonical_bytes(proposal["inventory"])
    prior = capture.files.get(relative)
    if prior is not None:
        try:
            require(len(prior) <= MAX_BYTES, "claims_target_foreign_container")
            sibling("_strict_contract").claims_document(json_document(prior))
        except (Exception, SystemExit):
            refuse("claims_target_foreign_container")
    ancestors = sorted(parent.as_posix() for parent in Path(relative).parents
                       if parent.as_posix() != "." and parent.as_posix() not in capture.directories)
    controls = {"files": [LOCK], "new_directories": [".locks"] if ".locks" not in capture.directories else []}
    checked_path(root, LOCK, field="claims_gate", kind="file")
    result = validate_document(RESULT, {"schema_version": RESULT, "status": "admitted", "proposal_sha256": file_hash(canonical_bytes(proposal)),
        "inventory_sha256": file_hash(raw), "claims_path": relative, "admitted_at": observed_at.isoformat(),
        "question_slugs": sorted(row["slug"] for row in proposal["inventory"]["questions"]),
        "original_ids": sorted(row["original_id"] for row in proposal["originals"]), "claim_ids": [row["id"] for row in proposal["inventory"]["claims"]],
        "mechanical_check_count": count, "semantic_approval": "not_evaluated", "accepted": False,
        "review_registered": False, "question_status_changed": False})
    plan = {"schema_version": PLAN, "proposal_sha256": result["proposal_sha256"], "path": relative,
        "prior_sha256": file_hash(prior) if prior is not None else None, "data": raw, "observed_at": observed_at.isoformat(),
        "configuration_sha256": content_id("draft-config", config), "policy_sha256": content_id("draft-policy", policy),
        "private_view": authority, "new_directories": ancestors, "controls": controls,
        "dependencies_sha256": dependencies(capture, relative, ancestors, controls), "report": result}
    check_inputs(root, plan)
    require(capture_workspace(root).files.get(relative) == prior, "claims_target_changed")
    return plan


@typed
def check_inputs(root, plan, *, publishing=False, _view=None):
    root, _context = _host(root)
    capture = capture_workspace(root)
    config = configuration(capture)
    policy = sibling("_strict_evidence").resolve_policy(root, config)
    require(policy is not None and target(root, config, policy) == plan["path"], "claims_policy_target_changed")
    require(content_id("draft-config", config) == plan["configuration_sha256"]
        and content_id("draft-policy", policy) == plan["policy_sha256"], "admission_policy_changed")
    temporary = None
    if publishing:
        candidates = [name for name, raw in capture.files.items() if Path(name).parent == Path(plan["path"]).parent
            and re.fullmatch(r"\.normalized-[a-f0-9]{32}", Path(name).name) is not None and raw == plan["data"]]
        require(len(candidates) == 1, "admission_publication_changed")
        temporary = candidates[0]
    require(dependencies(capture, plan["path"], plan["new_directories"], plan["controls"], temporary=temporary)
        == plan["dependencies_sha256"], "admission_inputs_changed")
    usage = sibling("_evidence_usage")
    selected = nullcontext(_view) if _view is not None else usage.current_view(root, config) if usage.configured(config) else nullcontext(None)
    with selected as view:
        require(private_view(view) == plan["private_view"], "source_authority_changed")
    return capture


@typed
def check_current_sources(root, proposal, plan, capture, view):
    config = configuration(capture)
    policy = sibling("_strict_evidence").resolve_policy(root, config)
    scopes = originals(capture, root, config, policy, proposal)
    _count, binding = source_checks(root, config, policy, capture, proposal, clock(current_time()), scopes, _live_view=view)
    sibling("_draft_computation").check(root, config, proposal, view=view)
    require(binding == plan["private_view"], "source_authority_changed")
    if view is not None:
        view.revalidate(root, config)
    closing = clock(current_time())
    if view is not None:
        require(closing >= view.now, "source_clock_regressed")
        view.now = closing
        for (revision, uses, purpose, consumer) in list(view.checked):
            require(view.check(revision, uses=list(uses), purpose=purpose, consumer=consumer)["eligible"], "source_use_not_permitted")
    for claim in proposal["inventory"]["claims"]:
        for evidence in claim["evidence"]:
            age = (closing - sibling("_evidence_authority").timestamp(evidence["observed_at"])).total_seconds()
            require(0 <= age <= policy["max_source_age_seconds"], "source_age_invalid")


@contextmanager
def claims_gate(root):
    require(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "native_claims_gate_unavailable")
    checked_path(root, ".locks", field="claims_gate_directory", kind="directory")
    checked_path(root, LOCK, field="claims_gate", kind="file")
    descriptors = []
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        try:
            os.mkdir(".locks", 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
        directory = os.open(".locks", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        descriptors.append(directory)
        try:
            gate = os.open("draft-admission.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            os.fsync(directory)
        except FileExistsError:
            gate = os.open("draft-admission.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(gate)
        info = os.fstat(gate)
        sibling("_host_evidence_store").private_entry(info)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 1, "claims_control_unowned")
        os.lseek(gate, 0, os.SEEK_SET)
        require(os.read(gate, 2) in {b"", b"\0"}, "claims_control_unowned")
        with sibling("_workspace_locks").descriptor_lock(gate, timeout_seconds=0):
            actual = os.stat("draft-admission.lock", dir_fd=directory, follow_symlinks=False)
            require((actual.st_dev, actual.st_ino) == (info.st_dev, info.st_ino), "claims_gate_changed")
            yield
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@typed
def commit_admission(root, proposal, plan):
    root, _context = _host(root)
    proposal = proposal_document(proposal)
    require(plan.get("schema_version") == PLAN and plan.get("proposal_sha256") == file_hash(canonical_bytes(proposal))
        and plan.get("data") == canonical_bytes(proposal["inventory"]), "admission_plan_changed")
    with claims_gate(root):
        capture = check_inputs(root, plan)
        prior = capture.files.get(plan["path"])
        require((file_hash(prior) if prior is not None else None) == plan["prior_sha256"], "claims_target_changed")
        config = configuration(capture)
        usage = sibling("_evidence_usage")
        with usage.current_view(root, config) if usage.configured(config) else nullcontext(None) as view:
            check_current_sources(root, proposal, plan, capture, view)
            def before_publish():
                current = check_inputs(root, plan, publishing=True, _view=view)
                check_current_sources(root, proposal, plan, current, view)
            sibling("_usage_materialization").publish_file(root, plan["path"], plan["data"], plan["prior_sha256"],
                before_publish=before_publish)
            require(check_inputs(root, plan, _view=view).files.get(plan["path"]) == plan["data"], "claims_publication_changed")
    return copy.deepcopy(plan["report"])


def run_admit_document(root, proposal, *, _plan=None):
    try:
        require(_plan is not None, "serialized_admission_required")
        return commit_admission(root, proposal, _plan)
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError, UnicodeError, yaml.YAMLError) as exc:
        if is_refusal(exc):
            raise
        refuse("admission_input_or_publication_uncertain")
