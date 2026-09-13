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
