#!/usr/bin/env python3
"""Safe YAML construction with optional LibYAML parsing for repeated reads."""

from __future__ import annotations

from typing import Any

import yaml

try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:  # PyYAML also supports installations without LibYAML.
    from yaml import SafeLoader


def safe_load(stream: str | bytes) -> Any:
    """Parse fresh input using only standard YAML constructors, without caching."""
    return yaml.load(stream, Loader=SafeLoader)


def safe_load_current(stream: str | bytes) -> Any:
    """Parse fresh configuration with the portable reader's acceptance boundary.

    LibYAML differs on literal tabs and YAML version directives. Select the
    portable parser for those inputs and let it classify accelerated refusals.
    No parsed configuration or current eligibility is retained.
    """
    markers = (b"\t", b"%YAML") if isinstance(stream, bytes) else ("\t", "%YAML")
    if any(marker in stream for marker in markers):
        return yaml.load(stream, Loader=yaml.SafeLoader)
    try:
        return safe_load(stream)
    except yaml.YAMLError:
        return yaml.load(stream, Loader=yaml.SafeLoader)


def compose_current(stream: str | bytes) -> Any:
    """Compose fresh nodes for lossless duplicate-key and alias inspection."""
    markers = (b"\t", b"%YAML") if isinstance(stream, bytes) else ("\t", "%YAML")
    if any(marker in stream for marker in markers):
        return yaml.compose(stream, Loader=yaml.SafeLoader)
    try:
        return yaml.compose(stream, Loader=SafeLoader)
    except yaml.YAMLError:
        return yaml.compose(stream, Loader=yaml.SafeLoader)


def split_frontmatter_block(text: str) -> tuple[str | None, str]:
    """Separate frontmatter at standalone delimiter lines, leaving YAML parsing to the caller."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None, text
    closing = next((index for index in range(1, len(lines)) if lines[index].rstrip(" \t") == "---"), None)
    if closing is None:
        return None, text
    return "\n".join(lines[1:closing]), "\n".join(lines[closing + 1:])
