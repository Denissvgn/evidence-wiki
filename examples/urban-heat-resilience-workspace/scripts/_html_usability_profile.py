"""Pure qualification of native HTML classification revisions, independent of admission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

HTML_USABILITY_VERSION = 1
HTML_USABILITY_FIELD = "html_usability_version"
NATIVE_HTML_PRODUCER = "normalize_sources.py"


@dataclass(frozen=True)
class HTMLUsabilityProfile:
    """Classification currency is separate from format validity and recorded usability."""

    state: str
    reason: str | None = None

    @property
    def applicable(self) -> bool:
        return self.state != "not_applicable"

    @property
    def requires_recheck(self) -> bool:
        return self.state not in {"current", "not_applicable"}


def valid_html_usability_version(value: Any) -> bool:
    """A revision is an exact positive integer, never a boolean or a coerced string."""
    return type(value) is int and value > 0


def evaluate_html_usability(
    frontmatter: Mapping[str, Any],
    *,
    source_kind: str | None = None,
    effective_method: str | None = None,
    current_revision: int = HTML_USABILITY_VERSION,
) -> HTMLUsabilityProfile:
    """Qualify a native HTML claim without reading, parsing, executing or modifying a source.

    Explicit foreign producers and consistent non-HTML records retain their own
    contracts. Missing identity and contradictory native HTML coordinates cannot
    turn into a foreign exemption. Legacy manual HTML requires native regeneration.
    """
    if not valid_html_usability_version(current_revision):
        raise ValueError("The current HTML usability revision must be a positive integer.")
    stored_kind = frontmatter.get("source_kind")
    kind = stored_kind if source_kind is None else source_kind
    method = frontmatter.get("extraction_method")
    candidate = kind == "html" or stored_kind == "html" or method == "html_text" or effective_method == "html"
    if not candidate:
        return HTMLUsabilityProfile("not_applicable")
    producer = frontmatter.get("normalizer")
    name = producer.get("name") if isinstance(producer, Mapping) else None
    name = name.strip() if isinstance(name, str) else ""
    if name and name not in {NATIVE_HTML_PRODUCER, "manual"}:
        return HTMLUsabilityProfile("not_applicable")
    revision = frontmatter.get(HTML_USABILITY_FIELD)
    if name != "manual" and valid_html_usability_version(revision) and revision > current_revision:
        # Preserve unknown future output even when another native coordinate is damaged.
        return HTMLUsabilityProfile("unsupported", "html_usability_profile_unsupported")
    if not name or kind != "html" or stored_kind != "html":
        return HTMLUsabilityProfile("invalid", "html_usability_profile_invalid")
    if effective_method not in (None, "html") or method not in ("html_text", None):
        return HTMLUsabilityProfile("invalid", "html_usability_profile_invalid")
    if method is None and name != "manual":
        return HTMLUsabilityProfile("invalid", "html_usability_profile_invalid")
    if HTML_USABILITY_FIELD in frontmatter and not valid_html_usability_version(frontmatter[HTML_USABILITY_FIELD]):
        return HTMLUsabilityProfile("invalid", "html_usability_profile_invalid")
    if name == "manual" or HTML_USABILITY_FIELD not in frontmatter:
        return HTMLUsabilityProfile("recheck_required", "html_usability_recheck_required")
    if revision < current_revision:
        return HTMLUsabilityProfile("recheck_required", "html_usability_recheck_required")
    return HTMLUsabilityProfile("current")
