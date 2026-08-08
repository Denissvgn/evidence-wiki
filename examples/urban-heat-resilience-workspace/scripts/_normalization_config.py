#!/usr/bin/env python3
"""Shared reader for the optional ``research.yml`` ``normalization:`` section.

The section maps manifest source kinds to an external normalizer command, so a
workspace whose evidence this package cannot extract — structured API payloads,
instrument output — can still produce normalized records through the normal pipeline
instead of having them written around it.

The section is optional and additive: when it is absent no adapter exists, normalization
behaves exactly as it did before the section existed, and
``compatible_research_yml_contract`` is unchanged. When the section is present but
malformed, the helpers raise ``NormalizationConfigError`` so each caller fails through
its own config-invalid path. A misconfigured adapter is never silently dropped: quietly
ignoring it would let a workspace believe its structured evidence had been normalized
when nothing ran.

Configuring an adapter authorizes this package to execute that command, which is why
the shape is strict. ``command`` must be an argv list, so the package never splits a
string into arguments on the operator's behalf. ``name`` and ``version`` must be
declared, so a record's stated producer can be checked against what the workspace
authorized rather than trusted from the adapter's own output. Kinds this package
normalizes itself cannot be adapter-mapped, so one config line can never silently
divert papers or PDFs away from the built-in extractors.

The package grants the adapter nothing: no network, no credentials, no writes of its
own. Whatever the adapter reaches, it reaches on its own authority — the same stance as
``integrations.retrieval.command``, and deliberately unlike
``integrations.codebase_analysis``, whose configured command this package never runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NORMALIZATION_SECTION_KEYS = frozenset({"adapters"})
ADAPTER_KEYS = frozenset({"kinds", "provider", "command", "name", "version", "timeout_seconds"})

ADAPTER_PROVIDER_COMMAND = "command"
ADAPTER_PROVIDERS = (ADAPTER_PROVIDER_COMMAND,)

DEFAULT_TIMEOUT_SECONDS = 120
# An adapter that never returns would hang normalization forever, so the timeout is
# bounded as well as positive. The ceiling is generous rather than tuned.
MAX_TIMEOUT_SECONDS = 3600

# Kinds `normalize_sources.py` extracts itself. Adapters exist for evidence it cannot
# read, so mapping one of these is refused rather than silently overriding a built-in
# extractor. Kept in step with `normalize_sources.normalization_method` by
# `tests/test_normalization_config.py`, which fails if this package learns or forgets
# how to normalize a kind. Accepting more kinds later is backward compatible;
# accepting them now and restricting later would break configs.
NATIVE_SOURCE_KINDS = frozenset(
    {
        "paper",
        "pdf",
        "repo_link",
        "web_link",
        "html",
        "table",
        "codebase_architecture",
    }
)

NORMALIZATION_CONFIG_REMEDIATION = (
    "Fix the research.yml normalization: section as documented in docs/research-yml.md."
)


class NormalizationConfigError(Exception):
    """Structured ``normalization:`` configuration failure with a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        remediation: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.remediation = remediation or NORMALIZATION_CONFIG_REMEDIATION


@dataclass(frozen=True)
class NormalizerAdapter:
    """One validated adapter declaration."""

    kinds: tuple[str, ...]
    provider: str
    command: tuple[str, ...]
    name: str
    version: str
    timeout_seconds: int

    def summary(self) -> dict[str, Any]:
        """Auditable description of what this workspace may execute."""
        return {
            "kinds": list(self.kinds),
            "provider": self.provider,
            "command": list(self.command),
            "name": self.name,
            "version": self.version,
            "timeout_seconds": self.timeout_seconds,
        }


def _unknown_keys(mapping: dict[str, Any], allowed: frozenset[str]) -> list[str]:
    return sorted(key for key in mapping if key not in allowed and not str(key).startswith("x-"))


def normalization_section(config: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``normalization:`` mapping, or an empty mapping when it is absent."""
    section = config.get("normalization") if isinstance(config, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise NormalizationConfigError("CONFIG_INVALID", "research.yml normalization must be a mapping.")
    unknown = _unknown_keys(section, NORMALIZATION_SECTION_KEYS)
    if unknown:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"research.yml normalization has unknown keys: {', '.join(unknown)}.",
        )
    return section


def _adapter_label(index: int) -> str:
    return f"research.yml normalization.adapters[{index}]"


def _validate_kinds(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label}.kinds must be a non-empty list of manifest source kinds.",
        )
    kinds: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise NormalizationConfigError(
                "CONFIG_INVALID",
                f"{label}.kinds entries must be non-empty strings; rejected value {item!r}.",
            )
        kind = item.strip()
        if any(character.isspace() for character in kind):
            raise NormalizationConfigError(
                "CONFIG_INVALID",
                f"{label}.kinds entries must not contain whitespace; rejected value {item!r}.",
            )
        # Membership is deliberately open: a domain pack may declare its own kinds, and
        # this reader validates shape rather than a closed vocabulary.
        if kind in NATIVE_SOURCE_KINDS:
            raise NormalizationConfigError(
                "CONFIG_INVALID",
                (
                    f"{label}.kinds may not claim {kind!r}: normalize_sources.py extracts that kind "
                    "itself. Adapters are for evidence this package cannot read."
                ),
            )
        if kind in kinds:
            raise NormalizationConfigError(
                "CONFIG_INVALID",
                f"{label}.kinds lists {kind!r} more than once.",
            )
        kinds.append(kind)
    return tuple(kinds)


def _validate_provider(value: Any, label: str) -> str:
    if value is None:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            (
                f"{label}.provider is required so research.yml states plainly that this workspace "
                f"executes a command; use one of: {', '.join(ADAPTER_PROVIDERS)}."
            ),
        )
    provider = value.strip() if isinstance(value, str) else value
    if provider not in ADAPTER_PROVIDERS:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label}.provider rejected value {value!r}; use one of: {', '.join(ADAPTER_PROVIDERS)}.",
        )
    return provider


def _validate_command(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, str):
        # Splitting a command string into arguments guesses at the operator's quoting.
        # An argv list says exactly what is executed, with no shell in the path.
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            (
                f"{label}.command must be a list of arguments, not a string, so argument "
                'boundaries are explicit; write ["tool", "--flag", "value"].'
            ),
        )
    if not isinstance(value, list) or not value:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label}.command must be a non-empty list of argument strings.",
        )
    command: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise NormalizationConfigError(
                "CONFIG_INVALID",
                f"{label}.command entries must be non-empty strings; rejected value {item!r}.",
            )
        command.append(item)
    return tuple(command)


def _validate_identity(value: Any, label: str, field: str) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            (
                f"{label}.{field} rejected value {value!r}; use a string. A YAML number would "
                'lose its exact form (1.40 becomes 1.4), so quote it: "1.40".'
            ),
        )
    if isinstance(value, int):
        # `version: 1` is unambiguous once stringified, so accept it rather than making
        # an operator quote a plain integer.
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label}.{field} must be a non-empty string identifying the adapter.",
        )
    return value.strip()


def _validate_timeout(value: Any, label: str) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int):
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label}.timeout_seconds must be a positive integer number of seconds; rejected value {value!r}.",
        )
    if value < 1 or value > MAX_TIMEOUT_SECONDS:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            (
                f"{label}.timeout_seconds must be between 1 and {MAX_TIMEOUT_SECONDS} seconds; "
                f"rejected value {value!r}."
            ),
        )
    return value


def _validate_adapter(value: Any, index: int) -> NormalizerAdapter:
    label = _adapter_label(index)
    if not isinstance(value, dict):
        raise NormalizationConfigError("CONFIG_INVALID", f"{label} must be a mapping.")
    unknown = _unknown_keys(value, ADAPTER_KEYS)
    if unknown:
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            f"{label} has unknown keys: {', '.join(unknown)}.",
        )
    return NormalizerAdapter(
        kinds=_validate_kinds(value.get("kinds"), label),
        provider=_validate_provider(value.get("provider"), label),
        command=_validate_command(value.get("command"), label),
        name=_validate_identity(value.get("name"), label, "name"),
        version=_validate_identity(value.get("version"), label, "version"),
        timeout_seconds=_validate_timeout(value.get("timeout_seconds"), label),
    )


def adapters(section: dict[str, Any]) -> tuple[NormalizerAdapter, ...]:
    """Validate ``normalization.adapters``; absent or empty means no adapter is configured."""
    value = section.get("adapters")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise NormalizationConfigError(
            "CONFIG_INVALID",
            "research.yml normalization.adapters must be a list of adapter declarations.",
        )
    validated = tuple(_validate_adapter(item, index) for index, item in enumerate(value))

    claimed: dict[str, int] = {}
    for index, adapter in enumerate(validated):
        for kind in adapter.kinds:
            if kind in claimed:
                raise NormalizationConfigError(
                    "CONFIG_INVALID",
                    (
                        f"{_adapter_label(index)}.kinds claims {kind!r}, which "
                        f"{_adapter_label(claimed[kind])} already claims. One kind resolves to "
                        "one adapter."
                    ),
                )
            claimed[kind] = index
    return validated


def normalization_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the validated normalization settings for one workspace config document."""
    return {"adapters": adapters(normalization_section(config))}


def adapter_for_kind(configured: tuple[NormalizerAdapter, ...], kind: Any) -> NormalizerAdapter | None:
    """Return the adapter that claims ``kind``, or ``None`` when no adapter does."""
    if not isinstance(kind, str) or not kind:
        return None
    for adapter in configured:
        if kind in adapter.kinds:
            return adapter
    return None


def adapter_summaries(configured: tuple[NormalizerAdapter, ...]) -> list[dict[str, Any]]:
    """Auditable description of every adapter a workspace may execute."""
    return [adapter.summary() for adapter in configured]
