"""Refuse native Windows before the package has any effect.

EvidenceWiki runs on Linux and macOS. The wheel installs anywhere, so the refusal
happens at import time; workspace scripts, which run without this package, raise
the same refusal line from ``_script_errors`` and their other import roots.
"""

from __future__ import annotations

PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
REFUSAL = "refused (PLATFORM_UNSUPPORTED): EvidenceWiki runs on Linux and macOS; native Windows is not supported."


class PlatformUnsupported(ImportError):
    """The host runs native Windows, which this release does not support."""

    error_code = PLATFORM_UNSUPPORTED


def require_supported(os_name: str) -> None:
    """Raise :class:`PlatformUnsupported` when ``os_name`` names native Windows."""
    if os_name == "nt":
        raise PlatformUnsupported(REFUSAL)
