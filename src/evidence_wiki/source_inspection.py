"""Installation/target capability facts kept separate from source usability."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import shutil
import tempfile
from pathlib import Path

import yaml

from ._script_host import shared_assets_root
from .agent import capabilities
from .errors import EvidenceWikiError
from .host_capabilities import describe, read_tools
from .pack_discovery import owner
from .source_contracts import refuse
from .source_inputs import SourceView
from .source_probe import provider_probe, registrations, tool_probe

DEPENDENCIES = ("PyYAML", "pypdf", "ruamel.yaml", "tzdata")
BUILTIN_CREDENTIALS = {"openalex": ("OPENALEX_API_KEY",), "github": ("GITHUB_TOKEN",)}
TARGET_CHECKERS = ("normalize_sources", "_host_capture", "_strict_evidence", "_computation_runtime")


def dependencies():
    result = []
    for name in DEPENDENCIES:
        try:
            version = importlib.metadata.version(name)
            state = "installed"
        except importlib.metadata.PackageNotFoundError:
            version, state = None, "missing"
        result.append({"distribution": name, "state": state, "version": version, "basis": "installation_metadata", "runtime_probe": "not_run"})
    return result


def provider_views(config, configured, probes=()):
    registry = owner("_provider_registry")
    declared, _, bounds = registrations()
    rows = []
    for phase, builtin in (("discovery", registry.DISCOVERY_PROVIDER_IDS), ("acquisition", registry.ACQUISITION_PROVIDER_IDS)):
        integrations = config.get("integrations")
        settings = integrations.get(phase, {}) if isinstance(integrations, dict) else {}
        settings = settings if isinstance(settings, dict) else {}
        candidates = [entry for entry in declared if entry["phase"] == phase]
        observed = {}
        for selection in probes:
            if selection.startswith(phase + ":"):
                selector = selection.split(":", 1)[1]
                result = provider_probe(phase, selector)
                observed[selector] = result
                if result.get("state") == "observed":
                    for entry in candidates:
                        if entry["selector"] == result["entry_point_selector"]:
                            entry["id"] = result["id"]
        configured_ids = settings.get("providers", [])
        if not isinstance(configured_ids, list) or not all(isinstance(value, str) for value in configured_ids):
            configured_ids = []
        ids = sorted(set(builtin) | {entry["id"] for entry in candidates} | set(configured_ids))
        valid = configured
        try:
            allowlist = registry.validate_provider_ids(settings.get("providers"), phase=phase,
                require_non_empty=settings.get("enabled") is True, registered=tuple(configured_ids))
            if settings.get("enabled") not in (None, True, False) or type(settings.get("enabled")) is int:
                valid = False
        except ValueError:
            valid = False
            allowlist = None
        for provider_id in ids:
            matches = [row for row in candidates if row["id"] == provider_id]
            built_in = provider_id in builtin
            collision = len(matches) > 1 or built_in and bool(matches)
            selected = allowlist is not None and provider_id in allowlist.providers
            observation = next((item for item in observed.values() if item.get("id") == provider_id), None)
            if observation is None and len(matches) == 1:
                observation = observed.get(matches[0]["selector"]) or observed.get(provider_id)
            refs = BUILTIN_CREDENTIALS.get(provider_id, ()) if built_in else ()
            if observation is not None and observation.get("state") == "observed":
                refs = observation["capabilities"]["credentials"]
            rows.append({"id": provider_id, "phase": phase, "kind": "built_in" if built_in else "registered_provider",
                "supported": "yes" if built_in else "observed_contract" if observation and observation.get("state") == "observed" else "unknown",
                "installed": "collision" if collision else "built_in" if built_in else "entry_point_present" if matches else "not_observed",
                "configured": "invalid" if configured and not valid else "selected" if selected else "not_selected" if configured else "unknown",
                "authorized": "configuration_allowed" if valid and selected and settings.get("enabled") is True and not collision and
                               (built_in or observation and observation.get("state") == "observed") else "selected_registration_unverified"
                               if valid and selected and settings.get("enabled") is True and not collision else "not_established",
                "credentials": {"basis": "built_in_optional_references" if built_in else "probed_declaration" if observation else "unknown",
                    "references": [{"name": name, "present": bool(os.environ.get(name))} for name in refs]},
                "connectivity": "not_probed", "capture": "not_demonstrated", "extraction": "source_dependent",
                "registration": matches[0] if len(matches) == 1 else None, "probe": observation,
                "request_kind_declarations_grant_routing": False})
    known = {f"{row['phase']}:{name}" for row in declared for name in (row["id"], row["entry_point"], row["selector"])}
    if set(probes) - known:
        refuse("provider_probe_unknown_or_builtin")
    return rows, bounds


def normalizer_views(config):
    module = owner("_normalization_config")
    try:
        adapters = module.normalization_config(config)["adapters"]
        declared = [{"name": item.name, "version": item.version, "kinds": list(item.kinds),
                     "configured": True, "executable_present": shutil.which(item.command[0]) is not None,
                     "executed": False, "output_compatibility": "not_demonstrated"} for item in adapters]
        state = "valid_declaration"
    except Exception:
        declared, state = [], "invalid_declaration"
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    backend = sources.get("pdf_extractor", "pypdf")
    available = any(row["distribution"] == "pypdf" and row["state"] == "installed" for row in dependencies()) if backend == "pypdf" else shutil.which("pdftotext") is not None if backend == "poppler" else False
    return {"native_kinds": sorted(module.NATIVE_SOURCE_KINDS), "adapters": declared, "configuration": state,
            "pdf": {"selected_backend": backend if backend in {"pypdf", "poppler"} else "invalid", "available": available,
                    "executed": False, "alternative": "pypdf" if backend != "pypdf" else None},
            "host_text_contract": owner("_host_capture").SCHEMA, "host_text_max_bytes": owner("_host_capture").MAX_BYTES,
            "native_extraction_executed": False, "bare_markdown": "unsupported_without_host_capture_or_adapter"}


def inspect(*, target=None, host_tools=None, source_ids=(), source_paths=(), probe_tools=(), probe_providers=(), _view=None):
    if len(source_ids) + len(source_paths) > 32 or len(probe_tools) > 2 or len(probe_providers) > 8:
        refuse("source_inspection_bound", "ONBOARDING_LIMIT")
    if len(set(probe_tools)) != len(probe_tools) or len(set(probe_providers)) != len(probe_providers):
        refuse("source_probe_duplicate")
    summary = capabilities()
    try:
        view = _view if _view is not None else SourceView(target)
    except (EvidenceWikiError, OSError, ValueError):
        view = SourceView(None)
        view.workspace = "invalid"
    tools = read_tools(host_tools)
    providers, provider_bounds = provider_views(view.config, view.workspace == "present", probe_providers)
    doctor = owner("doctor")
    python = doctor.python_check(doctor.DoctorEnvironment())
    contract = {"state": "not_inspected", "basis": "workspace marker"}
    if view.workspace == "present":
        with tempfile.TemporaryDirectory(prefix="evidence-wiki-marker-view-") as temporary:
            root = Path(temporary)
            (root / "workspace-system.yml").write_bytes(view.read("workspace-system.yml"))
            checked = doctor.contract_check(root, yaml)
        marker = checked.get("details", {})
        contract["state"] = "compatible" if checked["status"] == "ok" and marker.get("compatible_research_yml_contract") == summary["installation"]["research_contract_version"] else "incompatible_or_unknown"
    copied = []
    if view.workspace == "present":
        for name in TARGET_CHECKERS:
            relative = f"scripts/{name}.py"
            try:
                raw = view.read(relative)
            except (EvidenceWikiError, OSError, ValueError):
                copied.append({"id": name, "state": "unavailable_or_unsafe", "executed": False})
                continue
            expected = shared_assets_root() / "workspace-template" / relative
            with expected.open("rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            copied.append({"id": name, "state": "missing" if raw is None else "matching_package_bytes"
                           if hashlib.sha256(raw).hexdigest() == digest else "different_bytes", "executed": False})
    policy = view.config.get("strict_evidence")
    policy_state = "not_configured"
    if policy is not None:
        try:
            owner("_strict_contract").policy_document(policy)
            policy_state = "valid_declaration"
        except (ValueError, TypeError, KeyError):
            policy_state = "invalid_declaration"
    source_rows = []
    if source_ids or source_paths:
        from .source_readiness import inspect_sources

        source_rows = inspect_sources(view, source_ids=source_ids, source_paths=source_paths)
    demonstrations = [row["host_capture"] for row in source_rows if row.get("host_capture") is not None and row.get("delivery") == "captured"]
    result = {"schema_version": "evidence-source-inspection/v1", "installation": summary["installation"],
        "target": {"selected": target is not None, "state": view.workspace, "copied_checkers": copied, "contract": contract},
        "python": {key: python[key] for key in ("id", "status", "required", "version")},
        "dependencies": dependencies(), "tools": [{"id": name, "installed": shutil.which(name) is not None,
            "version": "not_probed", "executed": False} for name in ("git", "pdftotext", "pi", "opencode", "gemini")],
        "tool_probes": [tool_probe(name) for name in probe_tools], "providers": providers, "provider_inventory": provider_bounds,
        "normalization": normalizer_views(view.config), "host_tools": [describe(tool, demonstrations) for tool in tools],
        "strict": {"checker": summary["strict"]["checker"], "policy": policy_state,
            "requested_assurance": policy.get("assurance") if policy_state == "valid_declaration" else None,
            "review_authority": "not_verified", "receipt_authority": "not_verified", "host_protection": "not_probed", "effective_assurance": None},
        "computation": summary["computation"], "frameworks": {**summary["frameworks"], "local_runtime_version": "not_probed",
            "local_access": "not_probed", "host_protection": "not_verified"}, "sources": source_rows,
        "research_ready": False, "providers_enabled": False, "observation": view.finish(),
        "limitations": ["Installed metadata and declarations do not establish working access or permission.",
                        "Connectivity and host/reviewer authority were not verified.",
                        "Inspection does not initialize, acquire, inventory or normalize sources."]}
    return result
