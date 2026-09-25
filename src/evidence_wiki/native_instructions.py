"""Opt-in, reversible installation of canonical skills at explicit native roots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ._filesystem import os
from ._pack_io import canonical, identity, read_file
from .agent_resources import resource_document
from .extension_contracts import validate
from .frameworks import validate_bundle
from .local_journal import Journal
from .pack_authoring_store import child, protect, publish
from .pack_catalog import _writer_flags
from .planning_contracts import refuse
from .runtime_identity import generation

REQUEST = "evidence-native-instructions-request/v1"
PLAN = "evidence-native-instructions-plan/v1"
LOCATIONS = {
    "pi": {"project": ".pi/skills/evidence-wiki", "user": ".pi/agent/skills/evidence-wiki"},
    "opencode": {"project": ".opencode/skills/evidence-wiki", "user": ".config/opencode/skills/evidence-wiki"},
    "gemini": {"project": ".gemini/skills/evidence-wiki", "user": ".gemini/skills/evidence-wiki"},
}


def request(raw):
    value = validate(raw, REQUEST)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "framework", "version", "scope", "root"}
            or value["schema_version"] != REQUEST
            or any(not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in value.values())
            or value["framework"] not in LOCATIONS or value["scope"] not in {"project", "user"}):
        refuse("native_instructions_request_invalid")
    return value


def _files():
    bundle = json.loads(resource_document("framework/bundle/v1")["content"])
    validate_bundle(bundle)
    prefix = "skills/evidence-wiki/"
    files = {key[len(prefix):]: value.encode() for key, value in bundle["files"].items() if key.startswith(prefix)}
    files["LICENSE.txt"] = bundle["files"]["LICENSE.txt"].encode()
    return bundle, files


def _hashes(files):
    return {key: hashlib.sha256(raw).hexdigest() for key, raw in sorted(files.items())}


def plan(raw):
    selected = request(raw)
    matrix = json.loads(resource_document("framework/compatibility/v1")["content"])
    if not any(row["id"] == selected["framework"] and row["version"] == selected["version"] for row in matrix["frameworks"]):
        refuse("native_instructions_version_unqualified")
    root = Path(selected["root"]).expanduser().absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        refuse("native_instructions_root_requires_existing_canonical_directory")
    protect(root)
    bundle, files = _files()
    value = {"schema_version": PLAN, "request": {**selected, "root": str(root)}, "generation": generation(),
             "root_identity": identity(root), "relative_path": LOCATIONS[selected["framework"]][selected["scope"]],
             "files": _hashes(files), "instruction_sha256": bundle["instruction_sha256"],
             "activation": "host_discovery_and_trust_required", "global_configuration_changed": False}
    value["plan_id"] = "sha256:" + hashlib.sha256(canonical(value)).hexdigest()
    return value


def decode(raw):
    value = validate(raw, PLAN)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "request", "generation", "root_identity",
            "relative_path", "files", "instruction_sha256", "activation", "global_configuration_changed", "plan_id"}
            or value["schema_version"] != PLAN):
        refuse("native_instructions_plan_invalid")
    selected = request(canonical(value["request"]))
    if (value["relative_path"] != LOCATIONS[selected["framework"]][selected["scope"]]
            or value["plan_id"] != "sha256:" + hashlib.sha256(canonical({k: v for k, v in value.items() if k != "plan_id"})).hexdigest()
            or not isinstance(value["files"], dict) or not 1 <= len(value["files"]) <= 8):
        refuse("native_instructions_plan_changed", "ONBOARDING_PLAN_STALE")
    return value


def _observed(root):
    if root.is_symlink():
        refuse("native_instructions_directory_link")
    held = identity(root)
    files = {}
    for directory, directories, names in os.walk(root, followlinks=False):
        relative = Path(directory).relative_to(root).as_posix()
        if relative not in {".", "references"} or any((Path(directory) / name).is_symlink() for name in directories):
            refuse("native_instructions_unowned_directory", "ONBOARDING_TARGET_CONFLICT")
        if len(directories) > 1 or len(names) + len(files) > 16:
            refuse("native_instructions_tree_bound")
        for name in names:
            key = (Path(directory) / name).relative_to(root).as_posix()
            files[key] = read_file(root, key, maximum=131_072)
    if identity(root) != held:
        refuse("native_instructions_directory_changed", "ONBOARDING_PLAN_STALE")
    return files


def apply(raw, *, remove=False):
    value = decode(raw)
    root = Path(value["request"]["root"])
    protect(root)
    if root.is_symlink() or root.resolve() != root or identity(root) != value["root_identity"]:
        refuse("native_instructions_root_changed", "ONBOARDING_PLAN_STALE")
    if not remove and plan(canonical(value["request"])) != value:
        refuse("native_instructions_generation_changed", "ONBOARDING_PLAN_STALE")
    selected = root / value["relative_path"]
    archive_relative = ".evidence-wiki/instruction-archives/" + value["plan_id"][7:]
    archive = root / archive_relative
    record = canonical(value)
    journal = Journal(root, ".evidence-wiki/instruction-operations", value["plan_id"][7:], value)
    with journal.locked():
        root_fd = os.open(root, _writer_flags())
        parent = destination = directory = None
        try:
            parent = child(root_fd, Path(value["relative_path"]).parent.as_posix())
            if remove and not selected.exists() and archive.exists():
                observed = _observed(archive)
                if observed.pop("receipt.json", None) != record or _hashes(observed) != value["files"]:
                    refuse("native_instructions_archive_changed", "ONBOARDING_PLAN_STALE")
                return {"status": "already_removed", "archive": str(archive), "plan_id": value["plan_id"]}
            if remove:
                observed = _observed(selected)
                if observed.pop("receipt.json", None) != record or _hashes(observed) != value["files"]:
                    refuse("native_instructions_user_changes_preserved", "ONBOARDING_TARGET_CONFLICT")
                held = identity(selected)
                destination = child(root_fd, ".evidence-wiki/instruction-archives")
                try:
                    os.mkdir(archive.name, 0o700, dir_fd=destination)
                except FileExistsError:
                    if archive.is_symlink() or not archive.is_dir() or list(archive.iterdir()):
                        refuse("native_instructions_archive_exists", "ONBOARDING_TARGET_CONFLICT")
                if getattr(os, "native_windows", False):
                    # Windows cannot rename a directory over an empty directory.
                    # Release only the empty reservation; native rename refuses
                    # any destination that appears before publication.
                    os.rmdir(archive.name, dir_fd=destination)
                os.rename(selected.name, archive.name, src_dir_fd=parent, dst_dir_fd=destination)
                os.fsync(parent)
                os.fsync(destination)
                if identity(archive) != held or _observed(archive) != {**observed, "receipt.json": record}:
                    refuse("native_instructions_removal_changed", "ONBOARDING_PLAN_STALE")
                return {"status": "removed", "archive": str(archive), "plan_id": value["plan_id"]}
            if archive.exists():
                refuse("native_instructions_already_removed_new_plan_required", "ONBOARDING_TARGET_CONFLICT")
            _, files = _files()
            try:
                os.mkdir(selected.name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                created = False
            directory = os.open(selected.name, _writer_flags(), dir_fd=parent)
            if not created:
                observed = _observed(selected)
                if observed.get("receipt.json", observed.get("pending.json")) != record:
                    refuse("native_instructions_existing_user_directory", "ONBOARDING_TARGET_CONFLICT")
                if any(key not in {*files, "pending.json", "receipt.json"} or raw != (
                        record if key in {"pending.json", "receipt.json"} else files[key]) for key, raw in observed.items()):
                    refuse("native_instructions_user_changes_preserved", "ONBOARDING_TARGET_CONFLICT")
                if "receipt.json" in observed and "pending.json" not in observed and set(observed) == {*files, "receipt.json"}:
                    return {"status": "already_installed", "path": str(selected), "plan_id": value["plan_id"]}
            publish(directory, "pending.json", record)
            for name in sorted(files, key=lambda name: (name == "SKILL.md", name)):
                path = Path(name)
                nested = child(directory, path.parent.as_posix()) if path.parent.as_posix() != "." else os.dup(directory)
                try:
                    publish(nested, path.name, files[name])
                finally:
                    os.close(nested)
            if identity(root) != value["root_identity"] or _hashes(files) != value["files"]:
                refuse("native_instructions_inputs_changed", "ONBOARDING_PLAN_STALE")
            publish(directory, "receipt.json", record)
            os.unlink("pending.json", dir_fd=directory)
            os.fsync(directory)
            return {"status": "installed", "path": str(selected), "plan_id": value["plan_id"],
                    "activation": value["activation"], "global_configuration_changed": False,
                    "next": "Refresh the host's skill discovery and check collisions and trust separately."}
        finally:
            for descriptor in (directory, destination, parent, root_fd):
                if descriptor is not None:
                    os.close(descriptor)
