#!/usr/bin/env python3
"""Legacy selections for standalone scripts; trusted package loads bind a host bridge."""

from __future__ import annotations

import sys
from pathlib import Path

from _native_fs import os


def _active_bridge():
    # An independently imported legacy copy can coexist with a trusted package
    # call in one interpreter. Reuse an already loaded active selector without
    # importing a package into standalone execution.
    bridge = sys.modules.get("evidence_wiki.host_context")
    if bridge is not None and bridge.current_context() is not None:
        return bridge
    return None


def authority_path(project_root=None):
    bridge = _active_bridge()
    if bridge is not None:
        return bridge.authority_path(project_root)
    return os.environ.get("EVIDENCE_WIKI_AUTHORITY_FILE")


def state_path(project_root=None):
    bridge = _active_bridge()
    if bridge is not None:
        return bridge.state_path(project_root)
    return os.environ.get("EVIDENCE_WIKI_STATE_DIR")


def protected_paths():
    return tuple(Path(value) for value in (authority_path(), state_path()) if value)


def selection_identity():
    bridge = _active_bridge()
    if bridge is not None:
        return bridge.selection_identity()
    return ("EVIDENCE_WIKI_AUTHORITY_FILE", authority_path(), "EVIDENCE_WIKI_STATE_DIR", state_path())


def controller_allowed(role, controller):
    bridge = _active_bridge()
    if bridge is not None:
        return bridge.controller_allowed(role, controller)
    return True
