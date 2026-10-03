#!/usr/bin/env python3
"""Qualify actual workspace paths before scoped owner reads and mutations."""

from __future__ import annotations

import stat
from pathlib import Path

from _native_fs import os
from _script_errors import ScriptRefusal


def _refuse(reason, field):
    raise ScriptRefusal("WORKSPACE_PATH_REFUSED", "The workspace path cannot be used by this operation.",
        exit_code=2, recoverable=False, details={"reason": reason, "field": field},
        remediation="Use ordinary owned workspace paths without linked or escaped configuration, inputs, stores or locks.")


def checked_path(project_root: str | Path, value: str | Path, *, field: str = "path", kind: str | None = None) -> Path:
    """Check every existing component without following links or creating paths.

    Missing descendants are permitted for the canonical writer to create. This
    is a preflight, not an atomic transaction or isolation of another process.
    """
    descriptors = []
    try:
        if kind not in {None, "file", "directory"}:
            _refuse("workspace_path_kind_invalid", field)
        supplied_root = Path(project_root).expanduser().absolute()
        root = supplied_root.resolve(strict=True)
        path = Path(value)
        if ".." in path.parts or "\0" in str(path):
            _refuse("workspace_path_invalid", field)
        if path.is_absolute() and path.is_relative_to(supplied_root):
            path = root / path.relative_to(supplied_root)
        else:
            path = path if path.is_absolute() else root / path
        if not path.is_relative_to(root):
            _refuse("workspace_path_outside_root", field)
        if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            _refuse("workspace_path_platform_unsupported", field)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(root, flags)
        descriptors.append(descriptor)
        bindings = []
        parts = path.relative_to(root).parts
        if not parts and kind == "file":
            _refuse("workspace_path_kind_invalid", field)
        for index, component in enumerate(parts):
            try:
                observed = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(observed.st_mode):
                _refuse("workspace_path_link", field)
            if not stat.S_ISDIR(observed.st_mode) and not stat.S_ISREG(observed.st_mode):
                _refuse("workspace_path_special", field)
            if stat.S_ISREG(observed.st_mode) and observed.st_nlink != 1:
                _refuse("workspace_path_alias", field)
            if index == len(parts) - 1 and (
                kind == "file" and not stat.S_ISREG(observed.st_mode)
                or kind == "directory" and not stat.S_ISDIR(observed.st_mode)
            ):
                _refuse("workspace_path_kind_invalid", field)
            if index < len(parts) - 1:
                if not stat.S_ISDIR(observed.st_mode):
                    _refuse("workspace_path_parent_invalid", field)
                child = os.open(component, flags, dir_fd=descriptor)
                descriptors.append(child)
                held = os.fstat(child)
                if (observed.st_dev, observed.st_ino) != (held.st_dev, held.st_ino):
                    _refuse("workspace_path_changed", field)
                bindings.append((descriptor, component, held))
                descriptor = child
        for parent, component, held in bindings:
            current = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                _refuse("workspace_path_changed", field)
        return path
    except (OSError, ValueError, RuntimeError, TypeError):
        _refuse("workspace_path_unavailable", field)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
