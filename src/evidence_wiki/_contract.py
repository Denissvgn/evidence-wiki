"""The machine-readable capability contract for this installation.

``evidence-wiki contract`` and ``evidence_wiki.contract()`` answer the same
question -- what does this installation support -- so the payload is built once,
here, below both front ends. The CLI subcommand is now a ``json.dumps`` of this
function's return value; an embedding host calls it in-process instead of
spawning the CLI and parsing its stdout.

The module is private although the function it exports is the public entry point.
A submodule named ``contract`` cannot coexist with a package attribute named
``contract``: the import system binds ``evidence_wiki.contract`` to the *module*
the first time anything imports the submodule -- ``cli`` does, before any caller
reaches the package attribute -- and that binding pre-empts the lazy re-export in
``__init__``, so ``evidence_wiki.contract()`` would raise ``TypeError: 'module'
object is not callable`` depending on import order. Keeping the module private
leaves exactly one meaning for the public name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from . import __version__
from ._script_host import load_packaged_script, shared_assets_root
from .resources import STARTER_DIR, required_asset_manifest

CONTRACT_SCHEMA_VERSION = "1.0"

LIBRARY_API_VERSION = "1"

DOMAIN_PACK_STATE_SCHEMA_VERSION = "1.0"
DOMAIN_PACK_REFRESH_SCHEMA_VERSION = "1.0"
DOMAIN_PACK_LIFECYCLE_COMMANDS = ("pack refresh", "pack adopt")

# Operation names for the embeddable API, written as ``<namespace>.<operation>``:
# ``workspace.*`` are operations on a workspace handle, ``coverage``,
# ``grounding``, ``questions`` and ``normalize`` are the facades reached from a
# handle, ``orchestrate.session.*`` are the operations on the session that
# ``orchestrate.start`` returns, and the unqualified names are module-level
# functions on ``evidence_wiki`` itself.
#
# This list is a *declaration*, deliberately not introspected from live objects.
# Walking ``Workspace`` and its facades at call time would make the published
# contract depend on import order and on whichever half-built namespaces happened
# to be bound when the call landed, and it would silently widen or narrow the
# published API every time an internal helper was renamed. ``library_api.version``
# carries the compatibility signal instead: a host that understands version "1"
# knows exactly which names this list may contain, and a change to the surface
# that version "1" callers cannot absorb bumps the version rather than editing
# the list in place.
LIBRARY_API_SURFACE = (
    "workspace.open",
    "workspace.close",
    "workspace.versions",
    "workspace.status",
    "workspace.export_answers",
    "workspace.doctor",
    "coverage.evaluate",
    "grounding.verify",
    "normalize.verify",
    "questions.claim",
    "questions.release",
    "questions.answer",
    "questions.block",
    "questions.defer",
    "questions.reject",
    "questions.reopen",
    "questions.approve",
    "questions.review",
    "questions.set_grounding",
    "questions.add_batch",
    "orchestrate.start",
    "orchestrate.session.next",
    "orchestrate.session.submit",
    "orchestrate.session.status",
    "fleet_status",
    "contract",
)


def _normalized_overlay_sha256(overlay_path: Path, yaml_module: ModuleType) -> str:
    """Hash an overlay's data model rather than its presentation.

    Comments, key order, and quoting do not identify a pack revision. Refresh
    therefore uses the same canonical JSON material for its overlay identity so
    a presentation-only edit does not make a workspace appear out of date.
    """
    document = yaml_module.safe_load(overlay_path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _domain_pack_tree_sha256(pack_root: Path) -> str:
    """Return the content identity of every regular, non-symlink pack file."""
    digest = hashlib.sha256()
    paths = sorted(
        (
            path
            for path in pack_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.relative_to(pack_root).as_posix(),
    )
    for path in paths:
        relative = path.relative_to(pack_root).as_posix()
        file_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{relative}\0{file_sha256}\n".encode())
    return digest.hexdigest()


def _bundled_domain_pack_metadata(root: Path, yaml_module: ModuleType) -> dict[str, dict[str, str]]:
    """Describe the revisions bundled with this installation.

    The installed pack inventory used by policy discovery is deliberately left
    unchanged. This is a separate lifecycle-oriented view whose digest fields
    let tooling compare content without treating the display version as an
    ordered upgrade number.
    """
    domain_packs_root = root / "domain-packs"
    result: dict[str, dict[str, str]] = {}
    if not domain_packs_root.is_dir():
        return result
    for overlay_path in sorted(domain_packs_root.glob("*/research.overlay.yml")):
        try:
            document = yaml_module.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml_module.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        domain_pack = document.get("domain_pack")
        if not isinstance(domain_pack, dict):
            continue
        name = domain_pack.get("name")
        version = domain_pack.get("version")
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(version, str)
            or not version.strip()
        ):
            continue
        try:
            overlay_sha256 = _normalized_overlay_sha256(overlay_path, yaml_module)
            tree_sha256 = _domain_pack_tree_sha256(overlay_path.parent)
        except (OSError, TypeError, ValueError):
            continue
        result[name.strip()] = {
            "version": version.strip(),
            "overlay_sha256": overlay_sha256,
            "tree_sha256": tree_sha256,
        }
    return result


def _pack_policy_vocabularies(root: Path, coverage_module: ModuleType, yaml_module: ModuleType) -> dict[str, dict[str, dict[str, str]]]:
    domain_packs_root = root / "domain-packs"
    result: dict[str, dict[str, dict[str, str]]] = {}
    if not domain_packs_root.is_dir():
        return result
    for overlay_path in sorted(domain_packs_root.glob("*/research.overlay.yml")):
        try:
            document = yaml_module.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
        except yaml_module.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        domain_pack = document.get("domain_pack")
        if not isinstance(domain_pack, dict):
            continue
        name = domain_pack.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            vocabularies = coverage_module.domain_pack_policy_vocabularies({"domain_pack": domain_pack})
        except coverage_module.CoverageManifestError:
            continue
        if any(vocabularies.values()):
            result[name.strip()] = vocabularies
    return result


def _pack_policy_rules(
    root: Path, policy_primitives_module: ModuleType, yaml_module: ModuleType
) -> dict[str, dict[str, dict[str, Any]]]:
    """Walk installed domain packs for deterministic ``policy_rules`` declarations.

    Deliberately mirrors :func:`_pack_policy_vocabularies`: the same directory walk
    and the same per-pack failure posture -- missing directory, ``OSError``,
    ``YAMLError``, a non-dict document or ``domain_pack``, a blank name, and the
    primitives module's own :class:`PolicyRuleError` all ``continue`` rather than
    raise, so one broken pack never takes ``evidence-wiki contract`` down with it.
    No coverage module is needed here: ``pack_policy_rules`` already cross-checks a
    declared rule against the pack's own ``policy_vocabularies``, since both
    sections live in the single ``domain_pack`` mapping this walk reads once.
    """
    domain_packs_root = root / "domain-packs"
    result: dict[str, dict[str, dict[str, Any]]] = {}
    if not domain_packs_root.is_dir():
        return result
    for overlay_path in sorted(domain_packs_root.glob("*/research.overlay.yml")):
        try:
            document = yaml_module.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        except OSError:
            continue
        except yaml_module.YAMLError:
            continue
        if not isinstance(document, dict):
            continue
        domain_pack = document.get("domain_pack")
        if not isinstance(domain_pack, dict):
            continue
        name = domain_pack.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            rules = policy_primitives_module.pack_policy_rules({"domain_pack": domain_pack})
        except policy_primitives_module.PolicyRuleError:
            continue
        if not rules:
            continue
        # Summarized by the primitives module itself, so `evidence-wiki contract` and
        # `evidence-wiki pack validate` cannot drift into reporting the same pack
        # differently.
        result[name.strip()] = {
            policy_id: policy_primitives_module.rule_summary(rule) for policy_id, rule in rules.items()
        }
    return result


def contract() -> dict:
    """Return this installation's capability contract as a plain dict.

    The value is freshly built and caller-owned on every call: an in-process
    consumer that mutates it -- the published JSON Schema documents especially --
    must not be able to alter what the next caller sees.
    """
    import yaml

    from . import orchestration
    from .orchestration_schemas import public_orchestration_schema_documents

    # ``shared_assets_root()``, not a private ``with assets_root()`` block. A
    # library caller polls the contract or rebuilds it per workspace handle, and
    # on a zip install every separate entry re-extracts the whole asset tree and
    # seeds a disjoint script-module cache, since that cache is keyed by the
    # resolved script directory. Entering once per process bounds both. The
    # per-command ``with assets_root()`` entry survives only in the subcommands
    # that were never routed through the library API -- ``init``/``deploy``,
    # ``upgrade``, ``pack validate``, ``serve-mcp`` and ``orchestrator-guide``;
    # the ``contract`` subcommand is a one-shot process, so releasing this root at
    # interpreter exit is indistinguishable from releasing it at block exit, and
    # no path from the root reaches the payload, so stdout is unchanged either way.
    root = shared_assets_root()
    starter_root = root / STARTER_DIR
    metadata = yaml.safe_load((starter_root / "workspace-system.yml").read_text()) or {}
    workspace_system = metadata.get("workspace_system") if isinstance(metadata, dict) else {}
    if not isinstance(workspace_system, dict):
        workspace_system = {}
    initializer = load_packaged_script(root, "init_research_workspace")
    status_module = load_packaged_script(root, "workspace_status")
    intake_module = load_packaged_script(root, "intake_questions")
    export_module = load_packaged_script(root, "export_answers")
    source_requests_module = load_packaged_script(root, "source_requests")
    fetch_sources_module = load_packaged_script(root, "fetch_sources")
    verify_citations_module = load_packaged_script(root, "verify_citations")
    verify_quotes_module = load_packaged_script(root, "verify_quotes")
    discover_sources_module = load_packaged_script(root, "discover_sources")
    normalized_contract_module = load_packaged_script(root, "_normalized_contract")
    normalize_sources_module = load_packaged_script(root, "normalize_sources")
    mcp_module = load_packaged_script(root, "serve_mcp")
    script_errors_module = load_packaged_script(root, "_script_errors")
    provider_registry_module = load_packaged_script(root, "_provider_registry")
    question_claim_module = load_packaged_script(root, "question_claim")
    question_resolve_module = load_packaged_script(root, "question_resolve")
    run_report_module = load_packaged_script(root, "run_report")
    coverage_manifest_module = load_packaged_script(root, "coverage_manifest")
    publication_readiness_module = load_packaged_script(root, "publication_readiness")
    fleet_status_module = load_packaged_script(root, "fleet_status")
    policy_primitives_module = load_packaged_script(root, "_policy_primitives")
    base_policy_definitions = coverage_manifest_module.base_policy_vocabularies()
    installed_pack_policy_definitions = _pack_policy_vocabularies(root, coverage_manifest_module, yaml)
    installed_pack_policy_rules = _pack_policy_rules(root, policy_primitives_module, yaml)
    bundled_pack_metadata = _bundled_domain_pack_metadata(root, yaml)
    merged_policy_definitions = coverage_manifest_module.base_policy_vocabularies()
    for pack_vocabularies in installed_pack_policy_definitions.values():
        for field, definitions in pack_vocabularies.items():
            merged_policy_definitions.setdefault(field, {}).update(definitions)
    policy_vocabularies = {
        field: sorted(definitions)
        for field, definitions in merged_policy_definitions.items()
    }
    policy_vocabularies["artifact_kinds"] = sorted(coverage_manifest_module.ALLOWED_ARTIFACT_KINDS)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "package": "evidence-wiki",
        "package_version": __version__,
        "starter_version": workspace_system.get("starter_version"),
        "starter_schema_version": workspace_system.get("schema_version"),
        "compatible_research_yml_contract": workspace_system.get("compatible_research_yml_contract"),
        "profile_schema_versions": [initializer.PROFILE_SCHEMA_VERSION],
        "upgrade_compatibility": {
            "workspace_schema_versions": list(initializer.SUPPORTED_WORKSPACE_SCHEMA_VERSIONS),
            "research_yml_contract_versions": list(initializer.SUPPORTED_RESEARCH_YML_CONTRACTS),
        },
        "domain_pack_lifecycle": {
            "state_schema_version": DOMAIN_PACK_STATE_SCHEMA_VERSION,
            "refresh_schema_version": DOMAIN_PACK_REFRESH_SCHEMA_VERSION,
            "commands": list(DOMAIN_PACK_LIFECYCLE_COMMANDS),
            "bundled_packs": bundled_pack_metadata,
        },
        "orchestration_capabilities": {
            "managed_runner_ids": list(orchestration.managed_runner_names()),
            "external_protocol_commands": ["start", "next", "submit", "status"],
            "canonical_instruction_file": "AGENTS.md",
        },
        "library_api": {
            "version": LIBRARY_API_VERSION,
            "surface": list(LIBRARY_API_SURFACE),
        },
        "required_asset_manifest": required_asset_manifest(),
        "source_providers": {
            "discovery": list(provider_registry_module.DISCOVERY_PROVIDER_IDS),
            "acquisition": list(provider_registry_module.ACQUISITION_PROVIDER_IDS),
            "legacy_discovery_strategy_aliases": list(
                provider_registry_module.LEGACY_DISCOVERY_STRATEGY_IDS
            ),
        },
        "artifact_schemas": {
            "workspace_status": status_module.SCHEMA_VERSION,
            "question_intake": intake_module.SCHEMA_VERSION,
            "answer_export": export_module.SCHEMA_VERSION,
            "source_requests": source_requests_module.SCHEMA_VERSION,
            "fetch_sources": fetch_sources_module.SCHEMA_VERSION,
            "citation_verification": verify_citations_module.SCHEMA_VERSION,
            "quote_verification": verify_quotes_module.SCHEMA_VERSION,
            "discover_sources": discover_sources_module.SCHEMA_VERSION,
            "mcp_server": mcp_module.SCHEMA_VERSION,
            "question_claim": question_claim_module.SCHEMA_VERSION,
            "question_resolve": question_resolve_module.SCHEMA_VERSION,
            "run_state": "1.0",
            "orchestration_session": orchestration.ORCHESTRATION_SESSION_SCHEMA_VERSION,
            "orchestration_work_order": orchestration.ORCHESTRATION_WORK_ORDER_SCHEMA_VERSION,
            "orchestration_result": orchestration.ORCHESTRATION_RESULT_SCHEMA_VERSION,
            "orchestration_attempt": orchestration.ORCHESTRATION_ATTEMPT_SCHEMA_VERSION,
            "run_report": run_report_module.SCHEMA_VERSION,
            "coverage_manifest": coverage_manifest_module.SCHEMA_VERSION,
            "publication_readiness": publication_readiness_module.SCHEMA_VERSION,
            "fleet_status": fleet_status_module.SCHEMA_VERSION,
            "error_envelope": script_errors_module.SCHEMA_VERSION,
        },
        "normalized_source_format": {
            "version": normalized_contract_module.NORMALIZED_FORMAT_VERSION,
            "accepted_versions": sorted(normalized_contract_module.ACCEPTED_NORMALIZED_FORMATS),
            "violation_codes": list(normalized_contract_module.VIOLATION_CODES),
            "normalizer": {
                "name": normalize_sources_module.NORMALIZER_NAME,
                "version": normalize_sources_module.NORMALIZER_VERSION,
            },
            "contract_document": normalized_contract_module.CONTRACT_DOCUMENT,
        },
        "artifact_schema_documents": public_orchestration_schema_documents(),
        "policy_vocabularies": policy_vocabularies,
        "policy_vocabulary_definitions": {
            "base": base_policy_definitions,
            "installed_domain_packs": installed_pack_policy_definitions,
            "merged": merged_policy_definitions,
        },
        "policy_rules": installed_pack_policy_rules,
    }
