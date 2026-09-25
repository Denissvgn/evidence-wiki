"""Explicit pack discovery and immutable validation through the existing owners."""

from __future__ import annotations

import hashlib
import platform
import tempfile
from pathlib import Path

from . import __version__, domain_pack_validator
from ._pack_io import PackSnapshot, canonical, capture_pack, identity, read_file, refuse, signature, yaml_document
from ._script_host import load_packaged_script, shared_assets_root
from .agent_resources import installation_metadata
from .errors import EvidenceWikiError
from .resources import REQUIRED_DOMAIN_PACKS

SCHEMA = "evidence-pack-inspection/v1"
MAX_PACKS = 64


def owner(stem):
    return load_packaged_script(shared_assets_root(), stem)


def safe_name(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 128 or value in {".", ".."}
            or any(ord(char) < 32 or char in domain_pack_validator.FORBIDDEN_PACK_PATH_CHARACTERS or char == "/" for char in value)
            or value.endswith((" ", ".")) or value.split(".", 1)[0].casefold() in domain_pack_validator.WINDOWS_RESERVED_PACK_NAMES):
        refuse("pack_name_invalid")
    return value


def snapshot_metadata(snapshot: PackSnapshot) -> dict:
    overlay = yaml_document(snapshot.files["research.overlay.yml"])
    if not isinstance(overlay, dict) or not isinstance(overlay.get("domain_pack"), dict):
        refuse("pack_metadata_missing")
    pack = overlay["domain_pack"]
    safe_name(pack.get("name"))
    expected = installation_metadata()["research_contract_version"]
    info, checks = domain_pack_validator.metadata_check(pack, expected)
    scripts = domain_pack_validator.load_scripts(shared_assets_root() / "workspace-template")
    for field, validator in (("recommended_acquisition", domain_pack_validator.recommended_acquisition_check),
                             ("recommended_discovery", domain_pack_validator.recommended_discovery_check),
                             ("human_gated", domain_pack_validator.human_gated_check)):
        info[field], result = validator(pack)
        checks.append(result)
    for field, validator in (("policy_vocabularies", domain_pack_validator.policy_vocabularies_check),
                             ("policy_rules", domain_pack_validator.policy_rules_check),
                             ("request_kinds", domain_pack_validator.request_kinds_check)):
        info[field], result = validator(scripts, pack)
        checks.append(result)
    info["selection"] = owner("_pack_selection").selection_metadata(pack)
    info["description"] = pack.get("description") if isinstance(pack.get("description"), str) else None
    info["applicable_source_types"] = pack.get("applicable_source_types") if isinstance(pack.get("applicable_source_types"), list) else None
    info["coverage_templates"] = {} if pack.get("coverage_templates") is None else pack["coverage_templates"]
    if not isinstance(info["coverage_templates"], dict):
        refuse("pack_coverage_metadata_invalid")
    summaries = {}
    for name, relative in info["coverage_templates"].items():
        if not isinstance(relative, str) or relative not in snapshot.files:
            refuse("pack_coverage_reference_missing")
        content = yaml_document(snapshot.files[relative])
        summaries[name] = {"path": relative, "sha256": hashlib.sha256(snapshot.files[relative]).hexdigest(), "declaration": content}
    info["coverage_templates"] = summaries
    human = []
    for field in ("source_policy", "freshness_policy", "identity_policy"):
        for name in info["policy_vocabularies"].get(field, {}):
            rule = info["policy_rules"].get(name)
            if rule is None or rule.get("manual_review_required") or rule.get("manual_review_on_absence"):
                human.append(name)
    info["human_review_policies"] = sorted(human)
    info["providers_enabled"] = False
    info["compatible"] = info["compatible_research_yml_contract"] == expected
    info["metadata_checks"] = [{"id": check["id"], "status": check["status"]} for check in checks]
    info["metadata_valid"] = (info["name"] == snapshot.root.name
                              and all(check["status"] == "pass" for check in checks if check["id"] != "contract_compatibility"))
    info["identity"] = {"tree_sha256": snapshot.tree_sha256,
                        "overlay_sha256": owner("_domain_pack_lifecycle").overlay_sha256(overlay)}
    info["structural_validation"] = "not_run"
    return info


def inspect_pack(path: Path, *, origin: str, selector: str) -> dict:
    try:
        snapshot = capture_pack(path)
        metadata = snapshot_metadata(snapshot)
        return {"selector": selector, "origin": origin, "state": "available" if metadata["metadata_valid"] else "invalid", "name": metadata["name"],
                "metadata": metadata, "identity": metadata["identity"], "newer_revision": "unknown"}
    except (EvidenceWikiError, ValueError, TypeError, KeyError, RecursionError) as error:
        return {"selector": selector, "origin": origin, "state": "invalid", "name": None,
                "metadata": None, "identity": None, "newer_revision": "unknown",
                "reason": getattr(error, "details", {}).get("field", "pack_metadata_invalid")}


def _materialize(root: Path, files: dict[str, bytes]):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def inspect_installed(target: Path) -> tuple[dict, list[dict]]:
    """Run the lifecycle inspector against bounded private captured inputs."""
    root = target.expanduser().resolve(strict=True)
    controls, observations, snapshots = {}, {}, {}
    snapshot = None
    configured_name = None
    try:
        root_identity = identity(root)
        for name in ("research.yml", "workspace-system.yml", "domain-packs/.evidence-wiki-state.yml", "domain-packs/.evidence-wiki-transaction.yml"):
            path = root / name
            observations[name] = signature(path.lstat()) if path.exists() or path.is_symlink() else None
            if path.exists() or path.is_symlink():
                controls[name] = read_file(root, name, expected=observations[name])
                yaml_document(controls[name])
        config = yaml_document(controls.get("research.yml", b"{}"))
        names = set()
        if isinstance(config, dict) and isinstance(config.get("domain_pack"), dict):
            configured_name = safe_name(config["domain_pack"].get("name"))
            names.add(configured_name)
        state = yaml_document(controls.get("domain-packs/.evidence-wiki-state.yml", b"{}"))
        if isinstance(state, dict) and isinstance(state.get("pack"), dict):
            names.add(safe_name(state["pack"].get("name")))
        for name in sorted(names):
            path = root / "domain-packs" / name
            relative = "domain-packs/" + name
            observations[relative] = signature(path.lstat()) if path.exists() or path.is_symlink() else None
            if path.exists() or path.is_symlink():
                snapshots[name] = capture_pack(path)
        with tempfile.TemporaryDirectory(prefix="evidence-wiki-pack-view-") as temporary:
            captured = Path(temporary)
            _materialize(captured, controls)
            for name, item in snapshots.items():
                _materialize(captured / "domain-packs" / name, item.files)
            lifecycle = owner("_domain_pack_lifecycle").inspect_workspace(captured)
        for name, observed in observations.items():
            path = root / name
            now = signature(path.lstat()) if path.exists() or path.is_symlink() else None
            if now != observed:
                refuse("installed_pack_changed")
        for name, content in controls.items():
            if read_file(root, name) != content:
                refuse("installed_pack_changed")
        if identity(root) != root_identity or any(capture_pack(item.root).tree_sha256 != item.tree_sha256 for item in snapshots.values()):
            refuse("installed_pack_changed")
        snapshot = snapshots.get(lifecycle.get("name") or configured_name)
    except (EvidenceWikiError, ValueError, TypeError, KeyError, OSError):
        lifecycle = {"state": "state_invalid", "source_comparison_performed": False}
        snapshot = None
    lifecycle["newer_revision"] = "unknown"
    if lifecycle["state"] == "none":
        return lifecycle, []
    name = lifecycle.get("name") or configured_name or "unknown"
    selector = "installed:" + name
    if snapshot is None:
        row = {"selector": selector, "origin": "installed", "name": name, "state": "unavailable",
               "metadata": None, "identity": None, "newer_revision": "unknown"}
    else:
        try:
            metadata = snapshot_metadata(snapshot)
            row = {"selector": selector, "origin": "installed", "name": name, "state": "available" if metadata["metadata_valid"] else "invalid",
                   "metadata": metadata, "identity": metadata["identity"], "newer_revision": "unknown"}
        except (EvidenceWikiError, ValueError, TypeError, KeyError):
            row = {"selector": selector, "origin": "installed", "name": name, "state": "invalid", "metadata": None, "identity": None}
    row["lifecycle"] = lifecycle
    if lifecycle["state"] in {"state_invalid", "config_tree_skew", "transaction_incomplete"}:
        row["state"] = "invalid"
    return lifecycle, [row]


def inventory(*, target: str | Path | None = None, catalog: str | Path | None = None) -> dict:
    assets = shared_assets_root()
    rows = [inspect_pack(assets / "domain-packs" / name, origin="bundled", selector="bundled:" + name)
            for name in REQUIRED_DOMAIN_PACKS]
    lifecycle = None
    if target is not None:
        lifecycle, installed = inspect_installed(Path(target))
        rows.extend(installed)
    if catalog is not None:
        from .pack_catalog import entries

        rows.extend(entries(Path(catalog)))
    if len(rows) > MAX_PACKS:
        refuse("pack_inventory_bound")
    return {"schema_version": "evidence-pack-list/v1", "packs": rows, "workspace": lifecycle,
            "bounds": {"total": len(rows), "returned": len(rows), "truncated": False},
            "collisions": {name: [row["selector"] for row in rows if row["name"] == name]
                           for name in sorted({row["name"] for row in rows if row["name"]})
                           if sum(row["name"] == name for row in rows) > 1}}


def select(selector: str | None = None, *, target=None, catalog=None, path=None, resource=None) -> tuple[dict, Path]:
    if sum(value is not None for value in (selector, path, resource)) != 1:
        refuse("pack_selection_requires_one_locator")
    if path is not None:
        location = Path(path).expanduser().absolute()
        return inspect_pack(location, origin="path", selector="path"), location
    if resource is not None:
        known = {f"pack/bundled/{name}/v1": name for name in REQUIRED_DOMAIN_PACKS}
        if not isinstance(resource, str) or resource not in known:
            refuse("pack_resource_unknown", "ONBOARDING_RESOURCE_UNKNOWN")
        selector = "bundled:" + known[resource]
    if not isinstance(selector, str) or len(selector) > 256:
        refuse("pack_selector_invalid")
    if selector.startswith("bundled:"):
        name = selector.removeprefix("bundled:")
        if name not in REQUIRED_DOMAIN_PACKS:
            refuse("pack_not_found")
        location = shared_assets_root() / "domain-packs" / name
        return inspect_pack(location, origin="bundled", selector=selector), location
    if selector.startswith("installed:"):
        if target is None:
            refuse("installed_pack_requires_target")
        _, rows = inspect_installed(Path(target))
        matches = [row for row in rows if row["selector"] == selector]
        if not matches:
            refuse("pack_not_found")
        return matches[0], Path(target).expanduser().resolve() / "domain-packs" / matches[0]["name"]
    if selector.startswith("local:"):
        if catalog is None:
            refuse("local_pack_requires_catalog")
        from .pack_catalog import entries, resolve_entry

        key = selector.removeprefix("local:")
        matches = entries(Path(catalog), only=key)
        if not matches:
            refuse("pack_not_found")
        try:
            location = resolve_entry(Path(catalog), key)
        except EvidenceWikiError:
            location = Path(catalog)  # An unavailable descriptor is inspection-only.
        return matches[0], location
    view = inventory(target=target, catalog=catalog)
    rows = [row for row in view["packs"] if row["selector"] == selector or ":" not in selector and row["name"] == selector]
    if len(rows) != 1:
        refuse("pack_selection_ambiguous" if rows else "pack_not_found")
    row = rows[0]
    if row["origin"] == "bundled":
        location = shared_assets_root() / "domain-packs" / row["name"]
    elif row["origin"] == "installed":
        location = Path(target).expanduser().resolve() / "domain-packs" / row["name"]
    else:
        from .pack_catalog import resolve_entry

        location = resolve_entry(Path(catalog), row["selector"].removeprefix("local:"))
    return row, location


def checker_identity() -> str:
    import importlib.metadata

    import yaml

    digest = hashlib.sha256()
    digest.update(canonical([__version__, platform.python_implementation(), platform.python_version(), yaml.__version__]))
    for dependency in ("ruamel.yaml", "tzdata"):
        try:
            version = importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            version = None
        digest.update(canonical([dependency, version]))
    package = Path(__file__).parent
    for name in ("domain_pack_validator.py", "pack_discovery.py", "pack_catalog.py", "pack_decisions.py", "_pack_io.py", "pack_authoring.py", "pack_authoring_store.py",
                 "pack_authoring_contracts.py", "pack_assessment.py", "pack_qualification.py", "pack_acceptance.py"):
        digest.update(name.encode() + b"\0" + hashlib.sha256((package / name).read_bytes()).digest())
    template = shared_assets_root() / "workspace-template"
    for path in sorted(template.rglob("*")):
        relative = path.relative_to(template)
        if path.is_file() and "__pycache__" not in relative.parts and not any(part.startswith(".") for part in relative.parts):
            digest.update(relative.as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def validation_observation(path: Path, expected: str | None = None):
    """Run the canonical validator on captured bytes and retain failed findings."""
    snapshot = capture_pack(path)
    if expected is not None and expected != snapshot.tree_sha256:
        refuse("pack_revision_changed", "ONBOARDING_PLAN_STALE")
    checker = checker_identity()
    with tempfile.TemporaryDirectory(prefix="evidence-wiki-pack-validation-") as temporary:
        candidate = Path(temporary) / snapshot.root.name
        _materialize(candidate, snapshot.files)
        report = domain_pack_validator.validate_domain_pack(str(candidate), root=shared_assets_root())
    if capture_pack(path).tree_sha256 != snapshot.tree_sha256 or checker_identity() != checker:
        refuse("pack_changed_during_validation", "ONBOARDING_PLAN_STALE")
    return snapshot, report, checker


def validate_snapshot(path: Path, expected: str | None = None) -> tuple[dict, dict]:
    snapshot, report, checker = validation_observation(path, expected)
    if not report["ok"]:
        refuse("pack_canonical_validation_failed")
    metadata = snapshot_metadata(snapshot)
    receipt = {"schema_version": "evidence-pack-validation/v1", "tree_sha256": snapshot.tree_sha256,
               "overlay_sha256": metadata["identity"]["overlay_sha256"], "checker_sha256": checker,
               "ok": True, "checks": [{"id": row["id"], "status": row["status"]} for row in report["checks"]],
               "authority": "caller_local_structural_observation", "semantic_adequacy": "not_evaluated"}
    return metadata, receipt
