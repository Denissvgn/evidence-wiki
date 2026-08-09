#!/usr/bin/env python3
"""Diagnose whether a runner can operate a research wiki workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"
PYTHON_MINIMUM = (3, 10)
WORKSPACE_DIRS = {
    "root": ".",
    "raw": "raw",
    "sources": "sources",
    "wiki": "wiki",
    "scripts": "scripts",
    "docs": "docs",
}
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
from _normalization_config import NormalizationConfigError, adapter_summaries, normalization_config
from _orchestration_config import OrchestrationConfigError, is_delegated, orchestration_config
from _provider_plugins import (
    ACQUISITION_PHASE,
    DISCOVERY_PHASE,
    ENTRY_POINT_GROUPS,
    PROVIDER_PHASES,
    ProviderPluginError,
    registration_report,
    require_registration,
)
from _provider_registry import ACQUISITION_PROVIDER_IDS, DISCOVERY_ACCEPTED_IDS
from _script_errors import handle_system_exit, json_mode_requested
from _workspace_health import evaluate_workspace_health

REGISTERED_PROVIDERS_CHECK_ID = "registered_providers"

#: Ids each phase already accepts without any registration. Everything else in a
#: research.yml provider list has to be supplied by an installed distribution.
BUILT_IN_PROVIDER_IDS = {
    ACQUISITION_PHASE: frozenset(ACQUISITION_PROVIDER_IDS),
    DISCOVERY_PHASE: frozenset(DISCOVERY_ACCEPTED_IDS),
}


@dataclass
class DoctorEnvironment:
    python_version: tuple[int, int, int] = sys.version_info[:3]

    def import_yaml(self):
        import yaml

        return yaml

    def import_pypdf(self):
        import pypdf

        return pypdf

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def command_version(self, command: list[str]) -> str | None:
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=10)  # noqa: S603
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
        return text.splitlines()[0] if text else None

    def write_probe(self, directory: Path) -> tuple[bool, str | None]:
        try:
            with tempfile.NamedTemporaryFile(prefix=".evidence-wiki-doctor-", dir=directory, delete=True):
                pass
        except OSError as exc:
            return False, str(exc)
        return True, None

    def now_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check research workspace environment capabilities.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Research workspace root to diagnose. Defaults to current directory.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser.parse_args(argv)


def check_item(
    check_id: str,
    label: str,
    status: str,
    required: bool,
    message: str,
    implication: str,
    remediation: str,
    *,
    version: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "status": status,
        "required": required,
        "message": message,
        "implication": implication,
        "remediation": remediation,
    }
    if version is not None:
        item["version"] = version
    if details is not None:
        item["details"] = details
    return item


def python_check(env: DoctorEnvironment) -> dict[str, Any]:
    version_tuple = tuple(env.python_version[:3])
    version = ".".join(str(part) for part in version_tuple)
    ok = version_tuple >= PYTHON_MINIMUM
    return check_item(
        "python",
        "Python runtime",
        "ok" if ok else "missing",
        True,
        f"Python {version} is available." if ok else f"Python {version} is older than 3.10.",
        "All workspace scripts require Python 3.10 or newer.",
        "Run the tools with Python 3.10 or newer.",
        version=version,
    )


def pyyaml_check(env: DoctorEnvironment) -> tuple[dict[str, Any], Any | None]:
    try:
        yaml = env.import_yaml()
    except ImportError as exc:
        return (
            check_item(
                "pyyaml",
                "PyYAML import",
                "missing",
                True,
                f"PyYAML is not importable: {exc}",
                "YAML configuration, contract metadata, and workspace scripts cannot run.",
                "Install PyYAML, for example with `python3 -m pip install PyYAML`.",
            ),
            None,
        )
    version = getattr(yaml, "__version__", "unknown")
    return (
        check_item(
            "pyyaml",
            "PyYAML import",
            "ok",
            True,
            "PyYAML is importable.",
            "YAML configuration and workspace metadata can be read.",
            "No action required.",
            version=str(version),
        ),
        yaml,
    )


def pypdf_check(env: DoctorEnvironment) -> dict[str, Any]:
    try:
        pypdf = env.import_pypdf()
    except ImportError as exc:
        return check_item(
            "pypdf",
            "pypdf import",
            "missing",
            True,
            f"pypdf is not importable: {exc}",
            "The portable PDF normalization backend cannot run.",
            "Reinstall EvidenceWiki so its required pypdf dependency is present, for example with "
            "`python3 -m pip install --upgrade evidence-wiki`.",
        )
    version = getattr(pypdf, "__version__", "unknown")
    return check_item(
        "pypdf",
        "pypdf import",
        "ok",
        True,
        "pypdf is importable.",
        "PDF-only records can use the portable Python normalization backend.",
        "No action required.",
        version=str(version),
    )


def tool_check(
    env: DoctorEnvironment,
    *,
    name: str,
    label: str,
    version_args: list[str],
    missing_implication: str,
    ok_implication: str,
    remediation: str,
) -> dict[str, Any]:
    path = env.which(name)
    if not path:
        return check_item(
            name,
            label,
            "missing",
            False,
            f"`{name}` was not found on PATH.",
            missing_implication,
            remediation,
        )
    version = env.command_version([path, *version_args])
    return check_item(
        name,
        label,
        "ok",
        False,
        f"`{name}` is available at {path}.",
        ok_implication,
        "No action required.",
        version=version,
        details={"path": path},
    )


def poppler_check(env: DoctorEnvironment, *, required: bool = False) -> dict[str, Any]:
    path = env.which("pdftotext")
    if not path:
        remediation = (
            "Install Poppler with `apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS, "
            "or `conda install conda-forge::poppler` on Windows, and expose `pdftotext` on PATH; "
            "or set sources.pdf_extractor to pypdf. pip does not install the Poppler executable."
            if required
            else "No action is required. To enable the explicit Poppler compatibility backend, install Poppler "
            "with `apt install poppler-utils` on Debian/Ubuntu, `brew install poppler` on macOS, or "
            "`conda install conda-forge::poppler` on Windows. pip does not install the Poppler executable."
        )
        return check_item(
            "pdftotext",
            "Poppler pdftotext",
            "missing" if required else "ok",
            required,
            (
                "Configured Poppler PDF extractor requires `pdftotext`, but it was not found on PATH."
                if required
                else "Optional `pdftotext` compatibility backend was not found on PATH."
            ),
            (
                "PDF normalization cannot run until the configured backend is available."
                if required
                else "The required pypdf backend remains available for PDF-only normalization."
            ),
            remediation,
            details={"available": False, "selected": required},
        )
    version = env.command_version([path, "-v"])
    return check_item(
        "pdftotext",
        "Poppler pdftotext",
        "ok",
        required,
        (
            f"Configured Poppler PDF extractor is available at {path}."
            if required
            else f"Optional `pdftotext` compatibility backend is available at {path}."
        ),
        (
            "PDF-only records can use the configured Poppler backend."
            if required
            else "The explicit Poppler compatibility backend can be selected."
        ),
        "No action required.",
        version=version,
        details={"available": True, "path": path, "selected": required},
    )


def workspace_write_check(project_root: Path, env: DoctorEnvironment) -> dict[str, Any]:
    checked: list[str] = []
    missing: list[str] = []
    unwritable: dict[str, str] = {}

    for label, relative in WORKSPACE_DIRS.items():
        path = project_root if relative == "." else project_root / relative
        checked.append(label)
        if not path.is_dir():
            missing.append(label)
            continue
        writable, error = env.write_probe(path)
        if not writable:
            unwritable[label] = error or "write probe failed"

    ok = not missing and not unwritable
    details = {"checked": checked}
    if missing:
        details["missing"] = missing
    if unwritable:
        details["unwritable"] = unwritable
    return check_item(
        "workspace_write",
        "Workspace write permissions",
        "ok" if ok else "degraded",
        False,
        "Workspace directories are writable." if ok else "Some workspace directories are missing or not writable.",
        (
            "Workspace automation can create manifests, normalized sources, wiki pages, and reports."
            if ok
            else "Workspace automation may fail when writing manifests, normalized sources, wiki pages, or reports."
        ),
        "Run from an initialized workspace and fix directory ownership or permissions.",
        details=details,
    )


def contract_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    metadata_path = project_root / "workspace-system.yml"
    if not metadata_path.is_file():
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml was not found.",
            "Contract versions are unknown; orchestrators cannot confirm starter compatibility.",
            "Run from an initialized workspace or create one with `evidence-wiki init`.",
        )
    if yaml_module is None:
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml exists but cannot be parsed without PyYAML.",
            "Contract versions are unknown until PyYAML is installed.",
            "Install PyYAML and rerun doctor.",
        )
    try:
        document = yaml_module.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            f"workspace-system.yml could not be parsed: {exc}",
            "Contract versions are unknown; upgrade compatibility cannot be checked.",
            "Fix workspace-system.yml or restore it from the starter.",
        )
    workspace_system = document.get("workspace_system") if isinstance(document, dict) else None
    if not isinstance(workspace_system, dict):
        return check_item(
            "contract",
            "Workspace contract metadata",
            "degraded",
            False,
            "workspace-system.yml does not contain a workspace_system mapping.",
            "Contract versions are unknown; upgrade compatibility cannot be checked.",
            "Restore workspace-system.yml from the reusable starter.",
        )
    details = {
        "starter_version": workspace_system.get("starter_version"),
        "schema_version": workspace_system.get("schema_version"),
        "compatible_research_yml_contract": workspace_system.get("compatible_research_yml_contract"),
    }
    missing = [key for key, value in details.items() if not isinstance(value, str) or not value.strip()]
    return check_item(
        "contract",
        "Workspace contract metadata",
        "ok" if not missing else "degraded",
        False,
        "Workspace contract metadata is readable." if not missing else "Workspace contract metadata is incomplete.",
        (
            "Starter and research.yml contract versions can be compared before upgrades."
            if not missing
            else "Upgrade compatibility cannot be checked reliably."
        ),
        "No action required." if not missing else "Restore missing workspace_system fields.",
        details=details | ({"missing": missing} if missing else {}),
    )


def load_research_config(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    path = project_root / "research.yml"
    if yaml_module is None or not path.is_file():
        return {}
    try:
        document = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return document if isinstance(document, dict) else {}


def semantic_retrieval_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    config = load_research_config(project_root, yaml_module)
    integrations = config.get("integrations") if isinstance(config.get("integrations"), dict) else {}
    retrieval = integrations.get("retrieval") if isinstance(integrations.get("retrieval"), dict) else {}
    semantic = retrieval.get("semantic") if isinstance(retrieval.get("semantic"), dict) else {}
    if semantic.get("enabled") is not True:
        return check_item(
            "semantic_retrieval",
            "Semantic retrieval",
            "ok",
            False,
            "Semantic retrieval is disabled; lexical retrieval remains the default.",
            "Default retrieval stays deterministic and dependency-light.",
            "Enable integrations.retrieval.semantic only after configuring an operator-managed provider.",
            details={"enabled": False},
        )
    provider = semantic.get("provider")
    transport = semantic.get("transport", "command")
    details = {
        "enabled": True,
        "provider": provider if isinstance(provider, str) else None,
        "transport": transport if isinstance(transport, str) else None,
    }
    usable = isinstance(provider, str) and bool(provider.strip())
    if transport == "command":
        command = semantic.get("command")
        command_usable = (
            isinstance(command, str)
            and bool(command.strip())
            or isinstance(command, list)
            and all(isinstance(item, str) and item.strip() for item in command)
            and bool(command)
        )
        usable = usable and command_usable
    elif transport == "http":
        endpoint = semantic.get("endpoint")
        usable = usable and isinstance(endpoint, str) and endpoint.startswith(("http://", "https://"))
    else:
        usable = False
    return check_item(
        "semantic_retrieval",
        "Semantic retrieval",
        "ok" if usable else "degraded",
        False,
        "Semantic retrieval is configured." if usable else "Semantic retrieval is enabled but not usable.",
        (
            "Query mode can run best-effort hybrid lexical/semantic ranking."
            if usable
            else "Query mode will fall back to lexical retrieval until semantic provider settings are fixed."
        ),
        "Configure provider plus command or http endpoint, or disable integrations.retrieval.semantic.",
        details=details,
    )


def normalization_adapters_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    """Report which external commands normalization is authorized to execute.

    Doctor is where an auditor asks what a workspace can do before it does it, and a
    configured adapter is the one place normalization runs something the package did
    not ship. Listing the declarations — never running them — makes that visible
    without opening research.yml.
    """
    config = load_research_config(project_root, yaml_module)
    try:
        adapters = normalization_config(config)["adapters"]
    except NormalizationConfigError as exc:
        return check_item(
            "normalization_adapters",
            "Normalizer adapters",
            "degraded",
            False,
            f"The research.yml normalization section is invalid: {exc.message}",
            (
                "Normalization refuses to run at all while the section is invalid, so no source "
                "of any kind can be normalized."
            ),
            exc.remediation,
            details={"error_code": exc.error_code, "configured": 0, "adapters": []},
        )

    if not adapters:
        return check_item(
            "normalization_adapters",
            "Normalizer adapters",
            "ok",
            False,
            "No external normalizer adapters are configured.",
            "Normalization runs no external commands; only the packaged extractors run.",
            "No action required.",
            details={"configured": 0, "adapters": []},
        )

    summaries = adapter_summaries(adapters)
    kinds = sorted({kind for summary in summaries for kind in summary["kinds"]})
    return check_item(
        "normalization_adapters",
        "Normalizer adapters",
        "ok",
        False,
        f"{len(summaries)} external normalizer adapter(s) configured for: {', '.join(kinds)}.",
        "Normalization executes these commands for sources of the mapped kinds.",
        "Confirm each command is the reviewed tool you intended to authorize.",
        details={"configured": len(summaries), "adapters": summaries},
    )


def acquisition_mode_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    """Report who acquires evidence for this workspace.

    The same question doctor answers about normalizer adapters: what is this workspace
    authorized to do, read from the declaration rather than by watching it run. Under
    delegation the answer is "nothing" — the workspace fetches nothing itself — and the
    acquirer's identity is the thing an auditor needs to see.
    """
    config = load_research_config(project_root, yaml_module)
    try:
        settings = orchestration_config(config)
    except OrchestrationConfigError as exc:
        return check_item(
            "acquisition_mode",
            "Acquisition mode",
            "degraded",
            False,
            f"The research.yml orchestration section is invalid: {exc.message}",
            "Orchestration refuses to start a session while the section is invalid.",
            exc.remediation,
            details={"error_code": exc.error_code},
        )

    if not is_delegated(settings):
        return check_item(
            "acquisition_mode",
            "Acquisition mode",
            "ok",
            False,
            "This workspace acquires evidence through its own configured providers.",
            "Acquisition work orders are issued only for sources an enabled provider can fetch.",
            "No action required.",
            details={"acquisition_mode": settings["acquisition_mode"], "acquirer_agent_id": None},
        )

    return check_item(
        "acquisition_mode",
        "Acquisition mode",
        "ok",
        False,
        f"Acquisition is delegated to {settings['acquirer_agent_id']}.",
        (
            "The workspace fetches nothing itself: acquisition work orders are addressed to that acquirer, "
            "which supplies its own connectors, credentials, and egress policy. Managed runners refuse this "
            "workspace; drive it with orchestrate start/next/submit."
        ),
        "Confirm the named acquirer is the host you intended to authorize.",
        details={
            "acquisition_mode": settings["acquisition_mode"],
            "acquirer_agent_id": settings["acquirer_agent_id"],
            "max_attempts_per_request": settings["max_attempts_per_request"],
        },
    )


def authorized_provider_ids(config: dict[str, Any], phase: str) -> tuple[list[str], bool | None]:
    """Return the provider ids ``research.yml`` authorizes for one phase, and its switch.

    Read straight from the document rather than through the shared validator: doctor
    reports on workspaces whose configuration is broken, and a validator that refuses
    would take the whole section down with it. Anything that is not a plain non-empty
    string is not an authorization, so it is not counted as one.
    """
    integrations = config.get("integrations")
    section = integrations.get(phase) if isinstance(integrations, dict) else None
    if not isinstance(section, dict):
        return [], None
    providers = section.get("providers")
    ids = (
        [value.strip() for value in providers if isinstance(value, str) and value.strip()]
        if isinstance(providers, list)
        else []
    )
    enabled = section.get("enabled")
    return ids, enabled if isinstance(enabled, bool) else None


def _registration_entries(report: dict[str, Any], authorization: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every valid registration, stamped with whether research.yml authorizes it."""
    entries: list[dict[str, Any]] = []
    for phase in PROVIDER_PHASES:
        phase_report = report.get(phase) or {}
        group = phase_report.get("entry_point_group") or ENTRY_POINT_GROUPS[phase]
        authorized_ids = set(authorization[phase]["providers"])
        for record in phase_report.get("registered") or []:
            provider_id = record.get("id")
            authorized = provider_id in authorized_ids
            entries.append(
                {
                    "id": provider_id,
                    "phase": record.get("phase") or phase,
                    "distribution": record.get("distribution"),
                    "version": record.get("version"),
                    "entry_point": record.get("entry_point"),
                    "entry_point_group": group,
                    "provider_api_version": record.get("provider_api_version"),
                    "authorized": authorized,
                    # The CR's distinction, in one word an auditor can scan a column of:
                    # installing a distribution can only ever produce "available".
                    "state": "enabled" if authorized else "available",
                    "capabilities": record.get("capabilities") or {},
                }
            )
    entries.sort(key=lambda entry: (str(entry["id"]), str(entry["phase"])))
    return entries


def _invalid_entries(report: dict[str, Any], authorization: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten every registration that exists but cannot be used, with its reason."""
    entries: list[dict[str, Any]] = []
    for phase in PROVIDER_PHASES:
        phase_report = report.get(phase) or {}
        group = phase_report.get("entry_point_group") or ENTRY_POINT_GROUPS[phase]
        authorized_ids = set(authorization[phase]["providers"])
        for record in phase_report.get("invalid") or []:
            provider_id = record.get("id")
            entries.append(
                {
                    "id": provider_id,
                    "phase": record.get("phase") or phase,
                    "distribution": record.get("distribution"),
                    "entry_point": record.get("entry_point"),
                    "entry_point_group": group,
                    "authorized": provider_id in authorized_ids,
                    "reason": record.get("reason"),
                }
            )
    entries.sort(key=lambda entry: (str(entry["distribution"]), str(entry["entry_point"]), str(entry["id"])))
    return entries


def _unsatisfied_authorization_findings(
    authorization: dict[str, Any],
    seen: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Report every authorized id no valid registration supplies (backlog §2.7).

    The refusal text comes from ``require_registration`` rather than being written
    again here, so what doctor explains is word-for-word what smoke and the
    acquisition commands refuse with — including its split between *nothing supplies
    this id* and *something does and its declaration is broken*, which are two
    different fixes.
    """
    findings: list[dict[str, Any]] = []
    unsatisfied: list[dict[str, Any]] = []
    for phase in PROVIDER_PHASES:
        for provider_id in authorization[phase]["providers"]:
            if provider_id in BUILT_IN_PROVIDER_IDS[phase]:
                continue
            try:
                require_registration(phase, provider_id)
            except ProviderPluginError as exc:
                code, message, remediation, details = exc.error_code, exc.message, exc.remediation, exc.details
            except Exception as exc:  # pragma: no cover - the loader promises not to raise
                # A plugin that breaks enumeration is a finding about that plugin, never
                # a doctor that cannot report on the rest of the workspace.
                reason = " ".join(f"{type(exc).__name__}: {exc}".split())
                code = "PROVIDER_REGISTRATION_INVALID"
                message = f"Provider {provider_id!r} could not be resolved for {phase}: {reason}"
                remediation = f"Fix or uninstall the distribution registering {provider_id!r}, then rerun doctor."
                details = {"provider_id": provider_id, "phase": phase}
            else:
                continue
            unsatisfied.append({"phase": phase, "provider_id": provider_id})
            findings.append(
                {
                    "severity": "error",
                    "code": code,
                    "phase": phase,
                    "provider_id": provider_id,
                    "message": message,
                    "remediation": remediation,
                    "details": details,
                }
            )
            seen.add((phase, provider_id))
    return findings, unsatisfied


def _collision_findings(report: dict[str, Any], seen: set[tuple[str, str]]) -> list[dict[str, Any]]:
    """Report ids more than one installed distribution claims, naming every claimant.

    The loader refuses *both* sides of a duplicated id on purpose, so that behaviour
    cannot depend on installation order. That deliberate choice is invisible from the
    outside — the id simply is not there — unless doctor says who claimed it.
    """
    findings: list[dict[str, Any]] = []
    for phase in PROVIDER_PHASES:
        phase_report = report.get(phase) or {}
        valid_ids = {record.get("id") for record in phase_report.get("registered") or []}
        claims: dict[str, list[dict[str, Any]]] = {}
        for record in phase_report.get("invalid") or []:
            provider_id = record.get("id")
            if isinstance(provider_id, str) and provider_id:
                claims.setdefault(provider_id, []).append(record)
        for provider_id, records in sorted(claims.items()):
            distributions = sorted({str(record.get("distribution")) for record in records})
            if provider_id in valid_ids or len(distributions) < 2 or (phase, provider_id) in seen:
                continue
            findings.append(
                {
                    "severity": "error",
                    "code": "PROVIDER_REGISTRATION_INVALID",
                    "phase": phase,
                    "provider_id": provider_id,
                    "message": (
                        f"Provider id {provider_id!r} is claimed for {phase} by more than one installed "
                        f"distribution ({', '.join(distributions)}); every claim on a duplicated id is "
                        "refused, so the id is not available to research.yml."
                    ),
                    "remediation": f"Uninstall all but one of: {', '.join(distributions)}, then rerun doctor.",
                    "details": {
                        "provider_id": provider_id,
                        "phase": phase,
                        "distributions": distributions,
                        "reasons": [record.get("reason") for record in records],
                    },
                }
            )
            seen.add((phase, provider_id))
    return findings


def _invalid_registration_findings(
    invalid: list[dict[str, Any]],
    seen: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Report the unusable registrations no authorization is already waiting on.

    A warning rather than an error: the ids these would have supplied are not in any
    provider list, so nothing in the workspace is blocked. They are still listed,
    because a broken plugin that vanished silently would look exactly like one that was
    never installed — and those have different fixes.
    """
    findings: list[dict[str, Any]] = []
    for entry in invalid:
        provider_id = entry["id"]
        if isinstance(provider_id, str) and provider_id and (entry["phase"], provider_id) in seen:
            continue
        named = f"Provider {provider_id!r}" if provider_id else "An entry point"
        # An authorized id reaching here is one another registration already supplies;
        # saying "nothing depends on it" would be false for exactly that case.
        dependency = (
            f"research.yml authorizes {provider_id!r}, which a different installed registration supplies."
            if entry["authorized"]
            else "nothing in research.yml depends on it today."
        )
        findings.append(
            {
                "severity": "warning",
                "code": "PROVIDER_REGISTRATION_INVALID",
                "phase": entry["phase"],
                "provider_id": provider_id,
                "message": (
                    f"{named} registered for {entry['phase']} by {entry['distribution']} "
                    f"(entry point {entry['entry_point'] or 'unnamed'}) is unusable: {entry['reason']}"
                ),
                "remediation": f"Fix or uninstall {entry['distribution']}; {dependency}",
                "details": {
                    "provider_id": provider_id,
                    "phase": entry["phase"],
                    "distribution": entry["distribution"],
                    "entry_point": entry["entry_point"],
                    "reason": entry["reason"],
                },
            }
        )
    return findings


def registered_providers_check(project_root: Path, yaml_module: Any | None) -> dict[str, Any]:
    """List the third-party providers this environment makes available, and their declarations.

    Registration is packaging metadata: installing a distribution makes a provider id
    *available*, and only ``research.yml`` makes it *enabled*. Those are the two
    questions an auditor has — what could this workspace reach, and what did I
    authorize — and they have different answers, so this section prints both for every
    registration rather than one merged verdict.

    The check's own status is an operability verdict, not a defect count: it goes
    ``missing`` only when research.yml authorizes an id this environment cannot supply
    (smoke already refuses such a workspace, and doctor is where the operator learns
    why), ``degraded`` when something installed is broken but nothing depends on it,
    and ``ok`` otherwise. Individual defects carry their own severity in ``findings``.
    """
    config = load_research_config(project_root, yaml_module)
    authorization = {}
    for phase in PROVIDER_PHASES:
        providers, enabled = authorized_provider_ids(config, phase)
        authorization[phase] = {
            "enabled": enabled,
            "providers": providers,
            "entry_point_group": ENTRY_POINT_GROUPS[phase],
        }

    try:
        report = registration_report()
    except Exception as exc:  # pragma: no cover - the loader promises not to raise
        reason = " ".join(f"{type(exc).__name__}: {exc}".split())
        return check_item(
            REGISTERED_PROVIDERS_CHECK_ID,
            "Registered providers",
            "degraded",
            False,
            f"Installed provider registrations could not be enumerated: {reason}",
            "Doctor cannot say which third-party providers this environment makes available.",
            "Repair the installed distribution metadata for this environment, then rerun doctor.",
            details={
                "authorization": authorization,
                "registered": [],
                "invalid": [],
                "findings": [],
                "counts": {"registered": 0, "enabled": 0, "available": 0, "invalid": 0},
                "enumeration_error": reason,
            },
        )

    registered = _registration_entries(report, authorization)
    invalid = _invalid_entries(report, authorization)
    seen: set[tuple[str, str]] = set()
    findings, unsatisfied = _unsatisfied_authorization_findings(authorization, seen)
    findings.extend(_collision_findings(report, seen))
    findings.extend(_invalid_registration_findings(invalid, seen))

    enabled = [entry for entry in registered if entry["state"] == "enabled"]
    available = [entry for entry in registered if entry["state"] == "available"]
    counts = {
        "registered": len(registered),
        "enabled": len(enabled),
        "available": len(available),
        "invalid": len(invalid),
    }
    details = {
        "authorization": authorization,
        "registered": registered,
        "invalid": invalid,
        "findings": findings,
        "counts": counts,
    }

    if unsatisfied:
        named = ", ".join(f"{item['provider_id']} ({item['phase']})" for item in unsatisfied)
        return check_item(
            REGISTERED_PROVIDERS_CHECK_ID,
            "Registered providers",
            "missing",
            True,
            f"research.yml authorizes provider id(s) this environment cannot supply: {named}.",
            (
                "Smoke validation fails and the orchestration controller refuses to start a session "
                "until every authorized provider id is registered here."
            ),
            (
                "Install the distribution that registers each id into the environment that runs this "
                "workspace, or remove the id from the research.yml provider list."
            ),
            details=details,
        )

    if not registered and not invalid:
        return check_item(
            REGISTERED_PROVIDERS_CHECK_ID,
            "Registered providers",
            "ok",
            False,
            "No third-party providers are registered in this environment.",
            "This workspace can reach only the built-in providers research.yml authorizes.",
            "No action required.",
            details=details,
        )

    summary = f"{counts['registered']} registered provider(s): {counts['enabled']} enabled by research.yml, "
    summary += f"{counts['available']} available but not enabled."
    if invalid:
        summary += f" {counts['invalid']} installed registration(s) are unusable."
    return check_item(
        REGISTERED_PROVIDERS_CHECK_ID,
        "Registered providers",
        "degraded" if invalid else "ok",
        False,
        summary,
        (
            "Registration only makes a provider available; research.yml authorization is what enables it. "
            "An enabled provider may reach the domains it declares below, and nothing else."
            if not invalid
            else "Registration only makes a provider available; research.yml authorization is what enables it. "
            "The unusable registrations supply no provider id at all, so authorizing one would fail."
        ),
        (
            "Confirm each enabled provider is one you intended to authorize."
            if not invalid
            else "Confirm each enabled provider is one you intended to authorize, and fix or uninstall the "
            "distribution behind each unusable registration."
        ),
        details=details,
    )


def declared_credential_names(registered_providers: dict[str, Any]) -> tuple[str, ...]:
    """Return the credential variable *names* every valid registration declares.

    Names only — a :class:`CapabilitySummary` never carries a value, and this is the
    one path by which registration data reaches the secrets check.
    """
    names: set[str] = set()
    for entry in (registered_providers.get("details") or {}).get("registered") or []:
        for name in (entry.get("capabilities") or {}).get("credentials") or []:
            if isinstance(name, str) and name:
                names.add(name)
    return tuple(sorted(names))


def _env_file_variable_names(path: Path) -> set[str]:
    """Return the variable names assigned in a dotenv-style file, and nothing else.

    Only the text to the left of the first ``=`` on a line is ever kept. The value is
    dropped inside this function and never reaches a caller, a report, or a log.
    """
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return names
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export ") :].strip()
        if name:
            names.add(name)
    return names


def secret_exposure_check(project_root: Path, credential_names: tuple[str, ...] = ()) -> dict[str, Any]:
    candidates = [project_root / ".env"]
    readable: list[str] = []
    declared_names: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore"):
                pass
        except OSError:
            continue
        try:
            label = path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            label = path.resolve().as_posix()
        if label not in readable:
            readable.append(label)
        # Names only, and only the ones a registered provider declared: doctor has no
        # business enumerating an operator's whole environment, and never reads a value.
        if credential_names:
            declared_names |= _env_file_variable_names(path) & set(credential_names)

    exposed = sorted(declared_names)
    details: dict[str, Any] = {"readable_env_files": readable}
    if credential_names:
        details["declared_credentials"] = list(credential_names)
        details["exposed_credentials"] = exposed

    if exposed:
        message = (
            "Readable .env file(s) define declared provider credential name(s): "
            f"{', '.join(exposed)}; values were not inspected or printed."
        )
        implication = (
            "A credential a registered provider declares by name is defined in a repo-root .env, where it can "
            "leak into source/workspace state if .env files are treated as runtime configuration."
        )
        remediation = (
            f"Move {', '.join(exposed)} into the operator secret store, rotate the exposed key(s), and keep "
            "repo-root .env development-only."
        )
    elif readable:
        message = "Readable .env file(s) are present; values were not inspected or printed."
        implication = (
            "Provider credentials can leak into source/workspace state if .env files are treated as "
            "runtime configuration."
        )
        remediation = (
            "Move provider keys into the operator secret store, rotate exposed keys, "
            "and keep repo-root .env development-only."
        )
    else:
        message = "No readable .env file found at the workspace or invocation root."
        implication = "Operator-managed per-run environment injection remains the expected secret path."
        remediation = "No action required."

    return check_item(
        "secret_exposure",
        "Secret exposure",
        "degraded" if readable else "ok",
        False,
        message,
        implication,
        remediation,
        details=details,
    )


def verdict_for(checks: list[dict[str, Any]]) -> str:
    if any(check["required"] and check["status"] != "ok" for check in checks):
        return "missing"
    if any(check["status"] != "ok" for check in checks):
        return "degraded"
    return "ok"


def build_report(project_root: Path, env: DoctorEnvironment | None = None) -> dict[str, Any]:
    env = env or DoctorEnvironment()
    project_root = project_root.expanduser().resolve()
    pyyaml, yaml_module = pyyaml_check(env)
    pypdf = pypdf_check(env)
    config = load_research_config(project_root, yaml_module)
    sources = config.get("sources") if isinstance(config.get("sources"), dict) else {}
    poppler_required = sources.get("pdf_extractor", "pypdf") == "poppler"
    workspace_health = evaluate_workspace_health(
        project_root,
        optional_tool_availability={
            "pypdf": pypdf["status"] == "ok",
            "pdftotext": env.which("pdftotext") is not None,
        },
    )
    health_codes = ", ".join(workspace_health["finding_codes"]) or "none"
    registered_providers = registered_providers_check(project_root, yaml_module)
    checks = [
        python_check(env),
        pyyaml,
        pypdf,
        poppler_check(env, required=poppler_required),
        tool_check(
            env,
            name="git",
            label="Git",
            version_args=["--version"],
            missing_implication="Git-backed version-control workflows and user-edit snapshots are unavailable.",
            ok_implication="Version-control workflows and user-edit snapshots can use git.",
            remediation="Install git or run snapshot workflows without commit integration.",
        ),
        workspace_write_check(project_root, env),
        contract_check(project_root, yaml_module),
        semantic_retrieval_check(project_root, yaml_module),
        normalization_adapters_check(project_root, yaml_module),
        acquisition_mode_check(project_root, yaml_module),
        registered_providers,
        secret_exposure_check(project_root, declared_credential_names(registered_providers)),
        check_item(
            "workspace_health",
            "Shared workspace health",
            "ok" if not workspace_health["publication_blocked"] else "missing",
            True,
            f"Workspace health is {workspace_health['status']}; finding codes: {health_codes}.",
            "Every workspace-facing command consumes these same material validity findings.",
            (
                "Apply each finding's bounded remediation before treating the workspace as valid."
                if workspace_health["findings"]
                else "No action required."
            ),
            details=workspace_health,
        ),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": env.now_utc(),
        "project_root": project_root.as_posix(),
        "verdict": verdict_for(checks),
        "workspace_health": workspace_health,
        "checks": checks,
    }


def _joined(values: Any, empty: str = "none declared") -> str:
    items = [str(value) for value in values or [] if str(value)]
    return ", ".join(items) if items else empty


def _capability_lines(capabilities: dict[str, Any]) -> list[str]:
    rate_limit = capabilities.get("rate_limit")
    rate = (
        f"{rate_limit.get('requests')} request(s) per {rate_limit.get('per')}"
        if isinstance(rate_limit, dict)
        else "none declared"
    )
    return [
        f"      Declared domains: {_joined(capabilities.get('allowed_domains'))}",
        f"      Rate limit: {rate}",
        # Names, never values — stated in the label so nobody has to trust the code.
        f"      Credential names (values never read): {_joined(capabilities.get('credentials'))}",
        f"      Terms: {_joined(capabilities.get('terms_urls'))}",
        f"      Licence inference: {capabilities.get('license_inference') or 'unknown'}",
        f"      Request kinds: {_joined(capabilities.get('request_kinds'))}",
    ]


def registered_providers_lines(check: dict[str, Any]) -> list[str]:
    """Render the registered-provider detail that the one-line check summary cannot carry.

    Text is the format an operator actually reads, so the declaration an auditor is
    being asked to trust — the domains, the credential names, the rate ceiling — has to
    be on the page, not only in ``--format json``.
    """
    if check.get("id") != REGISTERED_PROVIDERS_CHECK_ID:
        return []
    details = check.get("details") or {}
    lines: list[str] = ["  Authorized in research.yml:"]
    for phase, block in (details.get("authorization") or {}).items():
        enabled = block.get("enabled")
        state = "enabled" if enabled is True else "disabled" if enabled is False else "unset"
        lines.append(f"    {phase} ({state}): {_joined(block.get('providers'), 'no providers listed')}")

    registered = details.get("registered") or []
    for state, heading in (
        ("enabled", "Enabled (registered here and authorized in research.yml)"),
        ("available", "Available (registered here, not authorized in research.yml)"),
    ):
        entries = [entry for entry in registered if entry.get("state") == state]
        if not entries:
            continue
        lines.append(f"  {heading}:")
        for entry in entries:
            lines.append(
                f"    {entry.get('id')} [{entry.get('phase')}] from {entry.get('distribution')} "
                f"{entry.get('version')} (entry point {entry.get('entry_point') or 'unnamed'}, "
                f"provider API v{entry.get('provider_api_version')})"
            )
            lines.extend(_capability_lines(entry.get("capabilities") or {}))

    invalid = details.get("invalid") or []
    if invalid:
        lines.append("  Invalid (installed here, supplying no provider id):")
        for entry in invalid:
            lines.append(
                f"    {entry.get('distribution')} [{entry.get('phase')}] "
                f"(entry point {entry.get('entry_point') or 'unnamed'}): {entry.get('reason')}"
            )

    findings = details.get("findings") or []
    if findings:
        lines.append("  Findings:")
        for finding in findings:
            lines.append(f"    {str(finding.get('severity')).upper()} {finding.get('code')}: {finding.get('message')}")
            lines.append(f"      Remediation: {finding.get('remediation')}")
    return lines


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Research Wiki Doctor",
        "====================",
        f"Project root: {report['project_root']}",
        f"Verdict: {report['verdict']}",
        "",
    ]
    for check in report["checks"]:
        marker = check["status"].upper()
        required = "required" if check["required"] else "optional"
        lines.append(f"- {marker} {check['label']} ({required}): {check['message']}")
        lines.append(f"  Implication: {check['implication']}")
        lines.append(f"  Remediation: {check['remediation']}")
        if check.get("version"):
            lines.append(f"  Version: {check['version']}")
        lines.extend(registered_providers_lines(check))
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None, env: DoctorEnvironment | None = None) -> int:
    args = parse_args(argv)
    json_mode = json_mode_requested(argv, default_json=args.format == "json")
    try:
        report = build_report(Path(args.project_root), env=env)
    except SystemExit as exc:
        return handle_system_exit(exc, json_mode=json_mode, default_exit_code=1)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print(render_text(report), end="")
    return 1 if report["verdict"] == "missing" else 0


if __name__ == "__main__":
    raise SystemExit(main())
