"""Declared write sets prevent setup from adopting unrelated concurrent changes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ._pack_io import read_file
from ._script_host import shared_assets_root
from .pack_discovery import owner
from .planning_contracts import refuse
from .planning_inputs import selected_pack
from .setup_worker import local_paths


def write_set(operation, plan, result):
    config = plan["initialization"]["effective_config"]
    files, directories, retained = set(), {""}, {}
    if operation == "initialize":
        init = owner("init_research_workspace")
        starter = shared_assets_root() / "workspace-template"
        for path in starter.rglob("*"):
            relative = path.relative_to(starter)
            if init.should_skip(relative):
                continue
            if path.is_file():
                files.add(relative.as_posix())
                retained[relative.as_posix()] = hashlib.sha256(read_file(starter, relative.as_posix(), 2_097_152)).hexdigest()
            elif path.is_dir():
                directories.add(relative.as_posix())
        profile = plan["profile"]["workspace_init"]
        mutable = {"research.yml", "docs/research-requirements.json", "index.md", "log.md"}
        mutable.update(value for value in (init.project_domain_guidance_path(profile), init.init_report_path(profile)) if value)
        pack, _ = selected_pack(plan["request"]["request"]["payload"]["domain"])
        if pack:
            for relative, raw in pack.files.items():
                path = "domain-packs/" + pack.root.name + "/" + relative
                files.add(path)
                retained[path] = hashlib.sha256(raw).hexdigest()
            mutable.add(init.DOMAIN_PACK_STATE_RELATIVE.as_posix())
        files.update(mutable)
        for path in mutable:
            retained.pop(path, None)
        directories.update(config["raw"]["source_roots"])
        directories.update(config["sources"][key] for key in ("normalized_dir", "cards_dir") if config["sources"].get(key))
        directories.add(config["wiki"]["root"])
        directories.update(config["wiki"]["root"] + "/" + value for value in config["wiki"]["required_dirs"])
        directories.add(config["outputs"]["default_dir"])
        if config["integrations"]["acquisition"]["enabled"]:
            directories.add(config["integrations"]["acquisition"]["target_root"])
        if config["integrations"]["discovery"]["enabled"]:
            directories.add(Path(config["integrations"]["discovery"].get("candidate_store_path", "sources/discovery/candidates.jsonl")).parent.as_posix())
    elif operation == "intake":
        files = {"index.md", "log.md", ".locks/log.lock", *[config["wiki"]["root"] + "/questions/" + row["slug"] + ".md" for row in plan["questions"]["rows"]]}
    elif operation == "coverage":
        module = owner("coverage_manifest")
        target = Path(plan["profile"]["workspace_init"]["target_path"])
        files = {module.manifest_path(target, config, row["question_slug"]).relative_to(target).as_posix() for row in plan["coverage"]}
    elif operation == "sources":
        files = {value for row in local_paths(plan) for value in (row["path"], row["path"] + ".provenance.yml")}
        if files:
            files.add("raw/.locks/acquisition.lock")
    elif operation == "inventory":
        files = {config["sources"]["manifest_path"], "raw/.locks/acquisition.lock"}
    elif operation == "normalize":
        module = owner("normalize_sources")
        for sid in result["selected"]:
            path = Path(config["sources"]["normalized_dir"]) / (module.safe_source_id(sid) + ".md")
            files.update((path.as_posix(), module.structured_view_path_for_record(path).as_posix()))
    for path in files | directories.copy():
        directories.update(parent.as_posix() for parent in Path(path).parents if parent.as_posix() != ".")
    return files, directories, retained


def verify_effects(operation, plan, result, before, after):
    allowed, allowed_directories, retained = write_set(operation, plan, result)
    before = before or {"files": [], "directories": []}
    old_files = {row["path"]: row for row in before["files"]}
    new_files = {row["path"]: row for row in after["files"]}
    for path in old_files.keys() | new_files.keys():
        if old_files.get(path) != new_files.get(path) and path not in allowed:
            refuse("setup_effect_outside_owned_write_set", "ONBOARDING_OWNERSHIP_CONFLICT")
    for path, expected in retained.items():
        if path not in new_files or new_files[path]["sha256"] != expected:
            refuse("setup_copied_asset_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
    old_dirs = {row["path"]: row for row in before["directories"]}
    new_dirs = {row["path"]: row for row in after["directories"]}
    for path in old_dirs.keys() | new_dirs.keys():
        if path not in old_dirs:
            if path not in allowed_directories:
                refuse("setup_directory_outside_write_set", "ONBOARDING_OWNERSHIP_CONFLICT")
        elif path not in new_dirs or old_dirs[path] != new_dirs[path]:
            # Initializer makes the preexisting empty target private.
            if not (operation == "initialize" and path == "" and path in new_dirs
                    and old_dirs[path]["identity"] == new_dirs[path]["identity"]):
                refuse("setup_owned_directory_changed", "ONBOARDING_OWNERSHIP_CONFLICT")
