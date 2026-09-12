#!/usr/bin/env python3
"""Scope an already authorized immutable capture across isolated script imports."""

from __future__ import annotations

import copy
import hashlib
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType

# Sibling loaders isolate ordinary modules. This private holder shares only a
# ContextVar for one exact implementation path and byte generation; contexts,
# threads, and different asset roots retain independent active captures.
_path = Path(__file__).resolve()
_key = "_evidence_wiki_capture_scope_" + hashlib.sha256(
    str(_path).encode() + b"\0" + _path.read_bytes()
).hexdigest()
_holder = ModuleType(_key)
_holder.active = ContextVar(_key, default=None)
_active = sys.modules.setdefault(_key, _holder).active


def captured_view(root, config):
    scope = _active.get()
    if scope is None or Path(root).resolve() != scope[0] or config != scope[1]:
        return None
    return scope[2]


def captured_config(config):
    scope = _active.get()
    return scope is not None and config == scope[1]


@contextmanager
def authorized_capture(root, config, view):
    """Borrow a lock-held host view only for a private, prequalified capture."""
    if _active.get() is not None:
        raise ValueError("publication_capture_scope_nested")
    token = _active.set((Path(root).resolve(), copy.deepcopy(config), view))
    try:
        yield
    finally:
        _active.reset(token)
