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
