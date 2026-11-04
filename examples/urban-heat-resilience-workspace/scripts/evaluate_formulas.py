#!/usr/bin/env python3
"""Evaluate declared formula graphs with isolated decimal arithmetic."""

import os

# Refuse before importing any sibling module, which would write its bytecode into the workspace.
if os.name == "nt":
    raise SystemExit(
        "refused (PLATFORM_UNSUPPORTED): EvidenceWiki runs on Linux and macOS; native Windows is not supported."
    )

from _computation_cli import main

if __name__ == "__main__":
    raise SystemExit(main(operation="evaluate"))
