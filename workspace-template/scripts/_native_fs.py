#!/usr/bin/env python3
"""Select native anchored filesystem operations without changing Python's os module."""

import os as _os

if _os.name == "nt":
    raise SystemExit(
        "refused (PLATFORM_UNSUPPORTED): EvidenceWiki runs on Linux and macOS; native Windows is not supported."
    )
os = _os
