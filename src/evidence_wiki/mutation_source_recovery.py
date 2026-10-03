"""Durable canonical source plans; staged bytes never authorize acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from ._filesystem import os
from ._pack_io import read_file, relative_path
from .mutation_contracts import RECEIPT, STATE, canonical, checked, detached, digest, refuse
from .operation_control import checkpoint, native_control
from .pack_discovery import owner

PLAN_VERSION = "research-source-effect-plan/v1"
MAX_ARTIFACT_BYTES = 16_777_216
MAX_STAGE_BYTES = 268_435_456
MAX_REPORT_BYTES = 1_048_576
MAX_EFFECTS = 3000
OPERATIONS = {
    "sources.inventory": ("source_inventory", "prepare_inventory_effects"),
    "normalize.sources": ("normalize_sources", "prepare_normalization_effects"),
}


def _hash(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _current(root, path):
    relative_path(path)
    target = root / path
    if not target.exists() and not target.is_symlink():
        return None
    return _hash(read_file(root, path, MAX_ARTIFACT_BYTES))


def _load(store, basis):
    stage = basis.get("source_stage", {})
    if stage.get("phase") != "ready" or stage.get("plan") is None:
        refuse("STAGING_INCOMPLETE", "/owner_basis/source_stage")
    raw = store.read_stage(stage["plan"])
    if not 0 < len(raw) <= MAX_ARTIFACT_BYTES:
        refuse("SOURCE_PLAN_LIMIT")
    try:
        def unique(pairs):
            data = {}
            for key, child in pairs:
                if key in data:
                    refuse("SOURCE_PLAN_INVALID")
                data[key] = child
            return data
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: refuse("SOURCE_PLAN_INVALID"))
    except (ValueError, UnicodeError, RecursionError):
        refuse("SOURCE_PLAN_INVALID")
    pending, nodes = [(value, 0)], 0
    while pending:
        child, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 500_000:
            refuse("SOURCE_PLAN_LIMIT")
        if type(child) is dict:
            if len(child) > 512:
                refuse("SOURCE_PLAN_LIMIT")
            pending.extend((item, depth + 1) for item in child.values())
        elif type(child) is list:
            if len(child) > 20_000:
                refuse("SOURCE_PLAN_LIMIT")
            pending.extend((item, depth + 1) for item in child)
        elif type(child) is int and child.bit_length() > 256 or type(child) is float and not math.isfinite(child):
            refuse("SOURCE_PLAN_INVALID")
    required = {"schema_version", "request_id", "input_sha256", "operation", "clock_utc", "effects",
        "inputs", "report", "strict_refusals", "allowed_directories", "selected_records"}
    if type(value) is not dict or set(value) != required or value["schema_version"] != PLAN_VERSION:
        refuse("SOURCE_PLAN_INVALID")
    if value["request_id"] != store.intent["request_id"] or value["input_sha256"] != store.intent["input_sha256"]:
        refuse("SOURCE_PLAN_CONFLICT")
    if value["operation"] not in OPERATIONS or type(value["effects"]) is not list or len(value["effects"]) > MAX_EFFECTS:
        refuse("SOURCE_PLAN_INVALID")
    owner("source_inventory").validate_plan_clock(value["clock_utc"])
    if value["clock_utc"] != stage["clock_utc"] or value["operation"] != store.intent["operation"]:
        refuse("SOURCE_PLAN_CONFLICT")
    paths = []
    for effect in value["effects"]:
        if type(effect) is not dict or set(effect) != {"path", "mode", "before_sha256", "before_bytes", "after_sha256", "stage"}:
            refuse("SOURCE_PLAN_INVALID")
        relative_path(effect["path"])
        if effect["mode"] not in {"write", "delete", "preserve"}:
            refuse("SOURCE_PLAN_INVALID")
        if (effect["mode"] == "write") != (effect["stage"] is not None):
            refuse("SOURCE_PLAN_INVALID")
        for key in ("before_sha256", "after_sha256"):
            if effect[key] is not None and (type(effect[key]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", effect[key]) is None):
                refuse("SOURCE_PLAN_INVALID")
        if effect["before_sha256"] is None:
            if effect["before_bytes"] is not None:
                refuse("SOURCE_PLAN_INVALID")
        elif type(effect["before_bytes"]) is not int or not 0 <= effect["before_bytes"] <= MAX_ARTIFACT_BYTES:
            refuse("SOURCE_PLAN_INVALID")
        if effect["mode"] == "delete" and effect["after_sha256"] is not None or (
                effect["mode"] == "preserve" and effect["after_sha256"] != effect["before_sha256"]):
            refuse("SOURCE_PLAN_INVALID")
        if effect["stage"] is not None:
            store.read_stage(effect["stage"])
        paths.append(effect["path"])
    if len(paths) != len(set(paths)):
        refuse("SOURCE_PLAN_INVALID")
    inputs = value["inputs"]
    if type(inputs) is not dict or set(inputs) != {"files", "entries", "roots", "absent"}:
        refuse("SOURCE_PLAN_INVALID")
    for kind, required in (("files", {"path", "sha256", "bytes"}), ("entries", {"path", "observation"}), ("roots", {"path", "observation"})):
        if type(inputs[kind]) is not list or len(inputs[kind]) > 20_000:
            refuse("SOURCE_PLAN_INVALID")
        names = set()
        for item in inputs[kind]:
            if type(item) is not dict or set(item) != required or type(item["path"]) is not str or item["path"] in names:
                refuse("SOURCE_PLAN_INVALID")
            names.add(item["path"])
            if item["path"] != ".":
                relative_path(item["path"])
            if kind == "files":
                if (type(item["sha256"]) is not str or re.fullmatch(r"sha256:[a-f0-9]{64}", item["sha256"]) is None
                        or type(item["bytes"]) is not int or not 0 <= item["bytes"] <= MAX_ARTIFACT_BYTES):
                    refuse("SOURCE_PLAN_INVALID")
            elif type(item["observation"]) is not list or not 7 <= len(item["observation"]) <= 16 or any(type(number) is not int for number in item["observation"]):
                refuse("SOURCE_PLAN_INVALID")
    if type(inputs["absent"]) is not list or len(inputs["absent"]) > 20_000:
        refuse("SOURCE_PLAN_INVALID")
    for path in inputs["absent"]:
        relative_path(path)
    if type(value["selected_records"]) is not list or len(value["selected_records"]) > 10_000 or any(type(row) is not dict for row in value["selected_records"]):
        refuse("SOURCE_PLAN_INVALID")
    if type(value["strict_refusals"]) is not list or len(value["strict_refusals"]) > 80_000:
        refuse("SOURCE_PLAN_INVALID")
    for row in value["strict_refusals"]:
        if (type(row) is not dict or set(row) not in ({"source_id", "reason", "message"}, {"source_id", "reason", "message", "path"})
                or any(type(text) is not str for text in row.values())
                or row["reason"] not in {"checksum_mismatch", "checksum_required"}):
            refuse("SOURCE_PLAN_INVALID")
    if type(value["allowed_directories"]) is not list or len(value["allowed_directories"]) > 4096:
        refuse("SOURCE_PLAN_INVALID")
    for path in value["allowed_directories"]:
        relative_path(path)
    if value["allowed_directories"] != sorted(set(value["allowed_directories"])):
        refuse("SOURCE_PLAN_INVALID")
    store.read_stage(value["report"])
    return value


def _dependencies(root, plan, *, after=False, operation_control=None):
    effects = {item["path"] for item in plan["effects"]}
    inputs = plan["inputs"]
    if type(inputs) is not dict or set(inputs) != {"files", "entries", "roots", "absent"}:
        refuse("SOURCE_PLAN_INVALID")
    for path in inputs["absent"]:
        checkpoint(operation_control, "before_absent_dependency")
        if after and path in effects:
            continue
        if (root / relative_path(path)).exists() or (root / path).is_symlink():
            refuse("SOURCE_INPUTS_CHANGED")
    for item in inputs["files"]:
        checkpoint(operation_control, "before_file_dependency")
        if after and item["path"] in effects:
            continue
        if _current(root, item["path"]) != item["sha256"]:
            refuse("SOURCE_INPUTS_CHANGED")
    revision = owner("_evidence_revision")
    for item in [*inputs["entries"], *inputs["roots"]]:
        checkpoint(operation_control, "before_path_dependency")
        if after and item["path"] in effects:
            continue
        path = root if item["path"] == "." else root / relative_path(item["path"])
        if list(revision.observation(path.lstat())) != item["observation"]:
            refuse("SOURCE_INPUTS_CHANGED")


def _quota(intent, basis, report):
    report = detached(report)
    state = {"schema_version": STATE, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "status": "owner_completed", "before_revision": intent["expected_revision"], "after_revision": None,
        "result": report, "error": None, "owner_basis": basis, "reconciliation": None}
    checked(STATE, state)
    checked(RECEIPT, {"schema_version": RECEIPT, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "operation": intent["operation"], "status": "committed", "before_revision": intent["expected_revision"],
        "after_revision": None, "result": report, "error": None,
        "historical": True, "authorized": False, "evidence_accepted": False})
    return report


def _refusal(refusals):
    if not refusals:
        return None
    module = owner("source_inventory")
    error = module.ScriptRefusal(module.strict_refusal_error_code(refusals), module.strict_refusal_message(refusals),
        exit_code=module.EXIT_STRICT_REFUSAL, details=module.strict_refusal_details(refusals))
    envelope = error.to_envelope()
    return {key: envelope[key] for key in ("error_code", "message", "recoverable", "remediation", "details")}


def _projected_quota(intent, basis, report, refusals):
    _quota(intent, basis, report)
    error = _refusal(refusals)
    proof = {"current_revision": intent["expected_revision"], "owner_observation": {
        "retained_result_sha256": digest(report), "retained_error_code": error["error_code"] if error else "MUTATION_INPUTS_CHANGED",
        "current_acceptance": "not_evaluated"}, "completion_proved": True, "desired_state_proved": False,
        "recovery_supported": True, "proof_kind": "owner_return", "operation_outcome": "owner_return_with_inputs_changed"}
    checked(STATE, {"schema_version": STATE, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "status": "partial", "before_revision": intent["expected_revision"], "after_revision": intent["expected_revision"],
        "result": report, "error": error, "owner_basis": basis, "reconciliation": proof})
    checked(RECEIPT, {"schema_version": RECEIPT, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "operation": intent["operation"], "status": "partial", "before_revision": intent["expected_revision"],
        "after_revision": intent["expected_revision"], "result": None if error else report, "error": error,
        "historical": True, "authorized": False, "evidence_accepted": False})


def _targets(root, operation, prepared, config):
    """Admit only separate declared source effects, never original/control data."""
    from .host_context import current_context

    sources = config.get("sources") if type(config.get("sources")) is dict else {}
    manifest = sources.get("manifest_path", "sources/manifest.jsonl")
    normalized = sources.get("normalized_dir", "sources/normalized")
    if type(manifest) is not str or type(normalized) is not str:
        refuse("SOURCE_TARGET_INVALID")
    relative_path(manifest)
    relative_path(normalized)
    if not manifest.startswith("sources/") or not normalized.startswith("sources/"):
        refuse("SOURCE_TARGET_UNSUPPORTED")
    protected = {"research.yml", "workspace-system.yml", "AGENTS.md", "CLAUDE.md", "GEMINI.md", "index.md"}
    wiki = config.get("wiki") if type(config.get("wiki")) is dict else {}
    wiki_path = Path(wiki.get("root", "wiki"))
    wiki_path = wiki_path if wiki_path.is_absolute() else root / wiki_path
    from .pack_catalog import _outside_assets
    trees = [root / item["path"] for item in prepared["inputs"]["roots"]]
    for effect in prepared["effects"]:
        path = relative_path(effect["path"])
        target = root / path
        current_context().require_path(target, write=True)
        _outside_assets(target, additional_roots=(Path(__file__).parent,))
        allowed = path == "log.md" or (operation == "sources.inventory" and path == manifest) or (
            operation == "normalize.sources" and target.is_relative_to(root / normalized) and
            (path.endswith(".md") or path.endswith(".structured.json")))
        if (not allowed or path in protected or target.is_relative_to(wiki_path.resolve())
                or path.startswith(("raw/", "scripts/", ".locks/", "runs/", "docs/"))):
            refuse("SOURCE_TARGET_UNSUPPORTED")
        if any(target.is_relative_to(tree) for tree in trees):
            refuse("SOURCE_INPUT_OUTPUT_OVERLAP")


def _projected_tree(root, effects):
    """Enforce the shared complete-revision profile before publishing outputs."""
    revision = owner("_evidence_revision")
    capture = revision.capture_workspace(root, max_attempts=1)
    files = {name: len(raw) for name, raw in capture.files.items()}
    directories = set(capture.directories)
    for effect in effects:
        path = effect["path"]
        if effect["mode"] == "delete":
            files.pop(path, None)
        elif effect["mode"] == "write":
            files[path] = len(effect["data"])
            directories.update(parent.as_posix() for parent in Path(path).parents if parent != Path("."))
    if len(files) > 10_000 or len(files) + len(directories) > 20_000 or sum(files.values()) > MAX_STAGE_BYTES:
        refuse("SOURCE_OUTPUT_LIMIT")


def prepare(root, intent, store, *, operation_control=None):
    """Retain clock/identity first, then stage every exact native publication."""
    checkpoint(operation_control, "before_source_plan_capture")
    state = store.read()
    basis = detached(state["owner_basis"])
    clock = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    basis["source_stage"] = {"phase": "preparing", "clock_utc": clock,
        "request_sha256": intent["input_sha256"], "configuration_sha256": basis["configuration_sha256"]}
    state["owner_basis"] = basis
    store.write(checked(STATE, state), expected_sha256=store.state_sha256())
    from .workspace import Workspace
    stem, seam = OPERATIONS[intent["operation"]]
    with Workspace.open(root) as workspace:
        module = workspace._script(stem)
        options = {"operation_control": native_control(operation_control, module)} if intent["operation"] == "normalize.sources" and operation_control is not None else {}
        prepared = getattr(module, seam)(root, intent["arguments"], observed_utc=clock, **options)
        config = module.load_config(root)
    _targets(root, intent["operation"], prepared, config)
    _projected_tree(root, prepared["effects"])
    if len(prepared["effects"]) > MAX_EFFECTS or len({item["path"] for item in prepared["effects"]}) != len(prepared["effects"]):
        refuse("SOURCE_PLAN_LIMIT")
    report = _quota(intent, basis, prepared["report"])
    total = len(canonical(report))
    for effect in prepared["effects"]:
        checkpoint(operation_control, "before_source_plan_effect")
        relative_path(effect["path"])
        raw = effect["data"]
        if raw is not None and (type(raw) is not bytes or len(raw) > MAX_ARTIFACT_BYTES):
            refuse("SOURCE_OUTPUT_LIMIT")
        total += len(raw) if raw is not None else 0
    if total > MAX_STAGE_BYTES - MAX_ARTIFACT_BYTES:
        refuse("SOURCE_PLAN_LIMIT")
    effects, directories = [], set()
    for index, effect in enumerate(prepared["effects"]):
        checkpoint(operation_control, "before_source_stage_publication")
        item = {key: value for key, value in effect.items() if key != "data"}
        item["stage"] = store.stage_blob(f"source-output-{index:04d}", effect["data"]) if effect["data"] is not None else None
        effects.append(item)
        path = Path(item["path"]).parent
        while path != Path(".") and not (root / path).exists():
            directories.add(path.as_posix())
            path = path.parent
    checkpoint(operation_control, "before_source_report_stage")
    report_stage = store.stage_blob("source-report", canonical(report))
    plan = {"schema_version": PLAN_VERSION, "request_id": intent["request_id"], "input_sha256": intent["input_sha256"],
        "operation": intent["operation"], "clock_utc": clock, "effects": effects,
        "inputs": prepared["inputs"], "report": report_stage, "strict_refusals": prepared["strict_refusals"],
        "allowed_directories": sorted(directories), "selected_records": prepared["selected_records"]}
    # The larger dependency inventory lives in private custody, not the envelope.
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_ARTIFACT_BYTES:
        refuse("SOURCE_PLAN_LIMIT")
    checkpoint(operation_control, "before_source_plan_publication")
    descriptor = store.stage_blob("source-plan", raw)
    basis["source_stage"].update(phase="ready", plan=descriptor)
    _projected_quota(intent, basis, report, prepared["strict_refusals"])
    checkpoint(operation_control, "before_source_plan_return")
    return basis


def _delete(root, effect, *, operation_control=None):
    """Use pinned no-follow directory handles for a sidecar deletion."""
    publisher = owner("_usage_materialization")
    parts = Path(relative_path(effect["path"])).parts
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for part in parts[:-1]:
            try:
                current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if effect["before_sha256"] is None:
                    return
                refuse("SOURCE_OUTPUT_CONFLICT")
            descriptors.append(current)
        try:
            previous = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        publisher.check_destination(root, descriptors, parts, previous)
        if previous is None:
            if effect["before_sha256"] is not None:
                refuse("SOURCE_OUTPUT_CONFLICT")
            return
        if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
            refuse("SOURCE_OUTPUT_CONFLICT")
        if _hash(read_file(current, parts[-1], MAX_ARTIFACT_BYTES)) != effect["before_sha256"]:
            refuse("SOURCE_OUTPUT_CONFLICT")
        publisher.check_destination(root, descriptors, parts, previous)
        checkpoint(operation_control, "before_source_delete")
        os.unlink(parts[-1], dir_fd=current)
        os.fsync(current)
        publisher.check_destination(root, descriptors, parts, None)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _report(store, plan):
    raw = store.read_stage(plan["report"])
    if len(raw) > MAX_REPORT_BYTES:
        refuse("SOURCE_REPORT_LIMIT")
    return detached(json.loads(raw.decode("utf-8")))


def invoke(root, intent, basis, store, *, operation_control=None):
    """Publish only retained bytes; extraction never repeats after preparation."""
    checkpoint(operation_control, "before_source_plan_load")
    plan = _load(store, basis)
    if plan["operation"] != intent["operation"]:
        refuse("SOURCE_PLAN_CONFLICT")
    _dependencies(root, plan, operation_control=operation_control)
    allowed = set(plan["allowed_directories"])
    for effect in plan["effects"]:
        checkpoint(operation_control, "before_source_output_capture")
        for parent in Path(effect["path"]).parents:
            if parent != Path(".") and not (root / parent).exists() and parent.as_posix() not in allowed:
                refuse("SOURCE_OUTPUT_CONFLICT")
    from .workspace import Workspace
    with Workspace.open(root) as workspace:
        module = workspace._script(OPERATIONS[intent["operation"]][0])
        config = module.load_config(root)
        module.require_host_intake(config, plan["selected_records"]) if intent["operation"] == "normalize.sources" else module.require_host_intake(config)
        publisher = owner("_usage_materialization")
        locks = owner("_workspace_locks")
        inventory = owner("source_inventory")
        with locks.workspace_lock(inventory.acquisition_workspace_lock_path(root), purpose="planned native source publication"):
            _dependencies(root, plan, operation_control=operation_control)
            # All current output states are checked before the first publication.
            for effect in plan["effects"]:
                checkpoint(operation_control, "before_source_output_validation")
                if _current(root, effect["path"]) != effect["before_sha256"]:
                    refuse("SOURCE_OUTPUT_CONFLICT")
            for effect in plan["effects"]:
                checkpoint(operation_control, "before_source_effect")
                _dependencies(root, plan, after=True, operation_control=operation_control)
                if effect["mode"] == "preserve":
                    continue
                if effect["mode"] == "delete":
                    options = {} if operation_control is None else {"operation_control": operation_control}
                    _delete(root, effect, **options)
                else:
                    raw = store.read_stage(effect["stage"])
                    if _hash(raw) != effect["after_sha256"]:
                        refuse("SOURCE_PLAN_CONFLICT")
                    if effect["path"] == "log.md":
                        with locks.workspace_lock(root / ".locks/log.lock", purpose="planned source activity"):
                            options = {} if operation_control is None else {
                                "before_publish": lambda: checkpoint(operation_control, "before_source_replace")}
                            publisher.publish_file(root, effect["path"], raw, effect["before_sha256"], **options)
                    else:
                        options = {} if operation_control is None else {
                            "before_publish": lambda: checkpoint(operation_control, "before_source_replace")}
                        publisher.publish_file(root, effect["path"], raw, effect["before_sha256"], **options)
    checkpoint(operation_control, "before_source_report_return")
    report = _report(store, plan)
    if plan["strict_refusals"]:
        module = owner("source_inventory")
        raise module.ScriptRefusal(module.strict_refusal_error_code(plan["strict_refusals"]),
            module.strict_refusal_message(plan["strict_refusals"]), exit_code=module.EXIT_STRICT_REFUSAL,
            details=module.strict_refusal_details(plan["strict_refusals"]))
    return report


def prove(root, intent, basis, store, *, operation_control=None):
    """Compare every planned publication and dependency without redispatch."""
    checkpoint(operation_control, "before_source_proof_capture")
    plan = _load(store, basis)
    from .workspace import Workspace
    with Workspace.open(root) as workspace:
        module = workspace._script(OPERATIONS[intent["operation"]][0])
        config = module.load_config(root)
        module.require_host_intake(config, plan["selected_records"]) if intent["operation"] == "normalize.sources" else module.require_host_intake(config)
    _dependencies(root, plan, after=True, operation_control=operation_control)
    report = _report(store, plan)
    matching, missing = [], []
    for effect in plan["effects"]:
        checkpoint(operation_control, "before_source_effect_proof")
        current = _current(root, effect["path"])
        (matching if current == effect["after_sha256"] else missing).append(effect["path"])
        if effect["stage"] is not None:
            store.read_stage(effect["stage"])
    partial = bool(plan["strict_refusals"]) or bool(report.get("summary", {}).get("failed") or report.get("summary", {}).get("partial"))
    complete = not missing
    observed_changes = False
    for effect in plan["effects"]:
        checkpoint(operation_control, "before_source_change_proof")
        if effect["mode"] != "preserve" and _current(root, effect["path"]) != effect["before_sha256"]:
            observed_changes = True
    checkpoint(operation_control, "before_source_proof_return")
    return {"present": observed_changes or complete and partial, "desired": complete and not partial, "completion": False,
        "observation": {"effects_complete": complete, "matching_count": len(matching), "missing_count": len(missing),
            "missing_or_changed_outputs": missing[:24], "output_listing_truncated": len(missing) > 24,
            "planned_operation_outcome": "partial" if partial else "committed", "report_stage": plan["report"],
            "original_execution_timing": "unknown", "strict_refusal_count": len(plan["strict_refusals"])},
        "operation_outcome": "desired_state_only" if complete and not partial else "original_unknown"}
