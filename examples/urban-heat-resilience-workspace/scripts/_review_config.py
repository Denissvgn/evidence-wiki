#!/usr/bin/env python3
"""Shared reader for the optional ``research.yml`` ``review:`` section.

The section decides how far one pending human review escalates. Under the
default ``workspace`` scope a question in ``human_review`` flips the workspace
readiness verdict to ``attention_required``, which freezes orchestration over
every other question. Under ``question`` scope the pending review parks only its
own question and is reported under a dedicated status counter instead.

The section is optional and additive: when it is absent every value falls back
to the documented default, so a workspace that never declares ``review:`` keeps
its previous semantics and the ``compatible_research_yml_contract`` stays
unchanged. When the section is present but malformed, the helpers raise
``ReviewConfigError`` so each caller fails through its own config-invalid path.
A misconfigured review section is not silently defaulted: quietly falling back
to ``workspace`` would freeze the very workspace the operator was unfreezing.
"""

from __future__ import annotations

from typing import Any

ESCALATION_SCOPE_WORKSPACE = "workspace"
ESCALATION_SCOPE_QUESTION = "question"
ESCALATION_SCOPES = (ESCALATION_SCOPE_WORKSPACE, ESCALATION_SCOPE_QUESTION)

DEFAULT_ESCALATION_SCOPE = ESCALATION_SCOPE_WORKSPACE
DEFAULT_MAX_PENDING_REVIEW_HOURS = 168

REVIEW_SECTION_KEYS = frozenset({"escalation_scope", "max_pending_review_hours"})
REVIEW_CONFIG_REMEDIATION = "Fix the research.yml review: section as documented in docs/research-yml.md."


class ReviewConfigError(Exception):
    """Structured ``review:`` configuration failure with a stable machine-readable code."""

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
        self.remediation = remediation or REVIEW_CONFIG_REMEDIATION


def review_section(config: dict[str, Any]) -> dict[str, Any]:
    """Return the raw ``review:`` mapping, or an empty mapping when it is absent."""
    section = config.get("review") if isinstance(config, dict) else None
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ReviewConfigError("CONFIG_INVALID", "research.yml review must be a mapping.")
    unknown = sorted(key for key in section if key not in REVIEW_SECTION_KEYS and not str(key).startswith("x-"))
    if unknown:
        raise ReviewConfigError(
            "CONFIG_INVALID",
            f"research.yml review has unknown keys: {', '.join(unknown)}.",
        )
    return section


def escalation_scope(section: dict[str, Any]) -> str:
    """Validate ``review.escalation_scope``; absent or empty keeps the workspace default."""
    value = section.get("escalation_scope")
    if value is None:
        return DEFAULT_ESCALATION_SCOPE
    scope = value.strip() if isinstance(value, str) else value
    if scope not in ESCALATION_SCOPES:
        raise ReviewConfigError(
            "CONFIG_INVALID",
            (
                f"research.yml review.escalation_scope rejected value {value!r}; "
                f"use one of: {', '.join(ESCALATION_SCOPES)}."
            ),
        )
    return scope


def max_pending_review_hours(section: dict[str, Any]) -> int | None:
    """Validate ``review.max_pending_review_hours``; explicit ``null`` disables the age check."""
    if "max_pending_review_hours" not in section:
        return DEFAULT_MAX_PENDING_REVIEW_HOURS
    value = section.get("max_pending_review_hours")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewConfigError(
            "CONFIG_INVALID",
            (
                "research.yml review.max_pending_review_hours must be a positive integer "
                f"number of hours or null; rejected value {value!r}."
            ),
        )
    return value


def review_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the validated review settings for one workspace config document."""
    section = review_section(config)
    return {
        "escalation_scope": escalation_scope(section),
        "max_pending_review_hours": max_pending_review_hours(section),
    }
