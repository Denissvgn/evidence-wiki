#!/usr/bin/env python3
"""Shared reader for the optional ``research.yml`` ``orchestration:`` section.

The section declares who acquires evidence for this workspace. Under the default
``providers`` mode the orchestration controller issues acquisition work orders only when
``integrations.acquisition.providers`` names a provider that can fetch the source, which
is correct for a workspace that does its own fetching. A host with its own connectors —
rate limits, credential custody, egress policy — disables those providers, and the
controller then has nobody to address acquisition to: the phase becomes unreachable and
fulfilment has to happen outside the protocol, unaccounted for by any work order.

``acquisition: delegated`` names an external acquirer instead. The controller issues
acquisition work orders addressed to that acquirer, and the acquirer fulfils requests
while its order is pending, so the mutation stays inside the protocol and keeps the
audit trail the integrity baseline exists to protect.

The section is optional and additive: when it is absent the workspace behaves exactly as
it did before the section existed, and ``compatible_research_yml_contract`` is unchanged.
When the section is present but malformed, the helpers raise ``OrchestrationConfigError``
so each caller fails through its own config-invalid path.

A misconfigured section is never silently defaulted, in either direction. Falling back to
``providers`` would leave a workspace that believes it delegates unable to issue any
acquisition order at all — research stops at ``blocked_on_sources`` while the operator
reads a config that says otherwise. Falling back to ``delegated`` would be worse: it would
address work orders to an acquirer nobody declared. Both failure modes are silent, so the
section fails loudly instead.

Delegated-only keys are refused under ``providers`` mode rather than ignored, for the same
reason. A config carrying ``acquirer_agent_id`` under ``providers`` states an intent the
workspace will not act on, and the operator who wrote it has no way to notice.
"""

from __future__ import annotations

from typing import Any

ACQUISITION_MODE_PROVIDERS = "providers"
ACQUISITION_MODE_DELEGATED = "delegated"
ACQUISITION_MODES = (ACQUISITION_MODE_PROVIDERS, ACQUISITION_MODE_DELEGATED)

DEFAULT_ACQUISITION_MODE = ACQUISITION_MODE_PROVIDERS

# How many recorded attempt failures retire one source request within a session. The
# router derives exhaustion from the durable attempt audit rather than a counter, so this
# is a bound on retries, not a stored tally. Two is one retry after the first failure.
DEFAULT_MAX_ATTEMPTS_PER_REQUEST = 2
# A delegated acquirer that keeps failing must stop being asked. The ceiling is generous
# rather than tuned; there is deliberately no "unlimited" value, because an unbounded
# retry budget turns a broken connector into a session that never terminates.
MAX_MAX_ATTEMPTS_PER_REQUEST = 10

# Mirrors `orchestration_controller.require_agent_id`: non-empty after stripping, bounded,
# and free of control characters, which is also what the published `AGENT_ID_PATTERN` in
# `src/evidence_wiki/orchestration_schemas.py` accepts. The rule lives here as a predicate
# because this reader cannot raise the controller's error type; the two are kept in step
# by `tests/test_orchestration_config.py`, which fails if either side changes alone.
MAX_AGENT_ID_LENGTH = 160

ORCHESTRATION_SECTION_KEYS = frozenset({"acquisition", "acquirer_agent_id", "max_attempts_per_request"})

# Keys only the delegated mode reads. Listing them once means a future delegated-only key
# is refused under `providers` by being added here, rather than by a new special case.
DELEGATED_ONLY_KEYS = ("acquirer_agent_id", "max_attempts_per_request")

ORCHESTRATION_CONFIG_REMEDIATION = (
    "Fix the research.yml orchestration: section as documented in docs/research-yml.md."
)


class OrchestrationConfigError(Exception):
    """Structured ``orchestration:`` configuration failure with a stable machine-readable code."""

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
        self.remediation = remediation or ORCHESTRATION_CONFIG_REMEDIATION


def valid_agent_id(value: Any) -> bool:
    """Return whether ``value`` is usable as an agent identifier.

    Shared predicate for the controller's ``--agent-id`` rule so a declared acquirer and
    a session owner cannot be validated by two drifting definitions.
    """
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return (
        bool(normalized)
        and len(normalized) <= MAX_AGENT_ID_LENGTH
        and not any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    )


def orchestration_section(config: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``orchestration:`` mapping, or an empty mapping when it is absent."""
    section = config.get("orchestration") if isinstance(config, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise OrchestrationConfigError("CONFIG_INVALID", "research.yml orchestration must be a mapping.")
    unknown = sorted(
        key for key in section if key not in ORCHESTRATION_SECTION_KEYS and not str(key).startswith("x-")
    )
    if unknown:
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            f"research.yml orchestration has unknown keys: {', '.join(unknown)}.",
        )
    return section


def acquisition_mode(section: dict[str, Any]) -> str:
    """Validate ``orchestration.acquisition``; absent keeps the providers default."""
    value = section.get("acquisition")
    if value is None:
        return DEFAULT_ACQUISITION_MODE
    mode = value.strip() if isinstance(value, str) else value
    if mode not in ACQUISITION_MODES:
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            (
                f"research.yml orchestration.acquisition rejected value {value!r}; "
                f"use one of: {', '.join(ACQUISITION_MODES)}."
            ),
        )
    return mode


def acquirer_agent_id(section: dict[str, Any], mode: str) -> str | None:
    """Validate ``orchestration.acquirer_agent_id``; required under delegation, refused otherwise."""
    value = section.get("acquirer_agent_id")
    if mode != ACQUISITION_MODE_DELEGATED:
        # Refusal is handled by `_reject_delegated_only_keys` so every delegated-only key
        # reports the same way; reaching here under providers means the key is absent.
        return None
    if value is None:
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            (
                "research.yml orchestration.acquirer_agent_id is required under "
                "acquisition: delegated, so work orders name the acquirer they are addressed to."
            ),
        )
    if not valid_agent_id(value):
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            (
                f"research.yml orchestration.acquirer_agent_id rejected value {value!r}; use a "
                f"non-empty string of at most {MAX_AGENT_ID_LENGTH} characters with no control characters."
            ),
        )
    return value.strip()


def max_attempts_per_request(section: dict[str, Any], mode: str) -> int:
    """Validate ``orchestration.max_attempts_per_request``; bounded, with no unlimited value."""
    if mode != ACQUISITION_MODE_DELEGATED:
        return DEFAULT_MAX_ATTEMPTS_PER_REQUEST
    if "max_attempts_per_request" not in section:
        return DEFAULT_MAX_ATTEMPTS_PER_REQUEST
    value = section.get("max_attempts_per_request")
    if isinstance(value, bool) or not isinstance(value, int):
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            (
                "research.yml orchestration.max_attempts_per_request must be a positive integer "
                f"number of attempts; rejected value {value!r}. There is no unlimited value: an "
                "unbounded retry budget would let one failing request keep a session alive forever."
            ),
        )
    if value < 1 or value > MAX_MAX_ATTEMPTS_PER_REQUEST:
        raise OrchestrationConfigError(
            "CONFIG_INVALID",
            (
                "research.yml orchestration.max_attempts_per_request must be between 1 and "
                f"{MAX_MAX_ATTEMPTS_PER_REQUEST}; rejected value {value!r}."
            ),
        )
    return value


def _reject_delegated_only_keys(section: dict[str, Any], mode: str) -> None:
    if mode == ACQUISITION_MODE_DELEGATED:
        return
    declared = [key for key in DELEGATED_ONLY_KEYS if key in section]
    if not declared:
        return
    raise OrchestrationConfigError(
        "CONFIG_INVALID",
        (
            f"research.yml orchestration declares {', '.join(declared)} under "
            f"acquisition: {mode}, where nothing reads {'them' if len(declared) > 1 else 'it'}. "
            "Set acquisition: delegated, or remove the delegated-only keys."
        ),
    )


def orchestration_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the validated orchestration settings for one workspace config document."""
    section = orchestration_section(config)
    mode = acquisition_mode(section)
    _reject_delegated_only_keys(section, mode)
    return {
        "acquisition_mode": mode,
        "acquirer_agent_id": acquirer_agent_id(section, mode),
        "max_attempts_per_request": max_attempts_per_request(section, mode),
    }


def is_delegated(settings: dict[str, Any]) -> bool:
    """Return whether validated settings declare an external acquirer."""
    return settings.get("acquisition_mode") == ACQUISITION_MODE_DELEGATED
