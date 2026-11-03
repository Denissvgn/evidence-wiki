#!/usr/bin/env python3
# ruff: noqa: I001, S101, UP007, UP035, UP045
# Preserve the pinned upstream validation implementation.
"""Offline packet validation from agent-wiki-cli 4.0.0: llm_wiki_cli.services.change_selection.

Original source SHA-256: 6d54b188661391b6644b99c573b1cbb961e1c900b60e5fd0b1eda78fe8a781aa
Only the validation dependency closure is included; source discovery, producer
execution, persistence, plugins and live reconciliation are excluded.

MIT License

Copyright (c) 2026 Denis Sivagin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

"""

from __future__ import annotations

import re


def portable_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Changed paths must be strings")
    value = value.replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    if (
        not value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Changed paths must be normalized source-relative paths")
    return value


def validate_changes(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("changes must be an object")
    mode = value.get("mode")
    fields = {
        "paths": {"mode", "paths"},
        "range": {"mode", "base", "head"},
        "staged": {"mode"},
    }
    if not isinstance(mode, str) or mode not in fields or set(value) != fields[mode]:
        raise ValueError("changes must specify paths, staged, or a base/head range")
    if mode == "paths":
        if not isinstance(value["paths"], list):
            raise ValueError("changes.paths must be an array")
        return {
            "mode": mode,
            "paths": sorted({portable_path(p) for p in value["paths"]}),
        }
    if mode == "range":
        for field in ("base", "head"):
            ref = value[field]
            if (
                not isinstance(ref, str)
                or not ref
                or ref.startswith("-")
                or any(ord(c) < 32 for c in ref)
            ):
                raise ValueError(f"changes.{field} must be a Git revision")
    return dict(value)
