"""Exclusive caller-local artifact trees using the existing anchored publishers."""

from __future__ import annotations

import os
from pathlib import Path

from ._pack_io import canonical, identity, relative_path
from .pack_authoring_store import child, protect, publish
from .pack_catalog import _writer_flags
from .planning_contracts import refuse


def create(output, files, record, *, closing=None):
    """Create a new tree; incomplete output is retained and never silently replaced."""
    target = Path(output).expanduser().absolute()
    protect(target)
    parent = target.parent.resolve(strict=True)
    target = parent / relative_path(target.name)
    before = identity(parent)
    parent_fd = os.open(parent, _writer_flags())
    directory = None
    try:
        protect(parent_fd)
        if identity(parent_fd) != before:
            refuse("artifact_parent_changed", "ONBOARDING_PLAN_STALE")
        try:
            os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            refuse("artifact_output_exists", "ONBOARDING_TARGET_CONFLICT")
        directory = os.open(target.name, _writer_flags(), dir_fd=parent_fd)
        held = identity(directory)
        publish(directory, "pending.json", canonical(record))
        for name, raw in sorted(files.items()):
            relative = Path(relative_path(name))
            nested = child(directory, relative.parent.as_posix()) if relative.parent.as_posix() != "." else os.dup(directory)
            try:
                publish(nested, relative.name, raw)
            finally:
                os.close(nested)
        if closing:
            closing(target)
        if identity(parent) != before or identity(target) != held:
            refuse("artifact_output_changed", "ONBOARDING_PLAN_STALE")
        publish(directory, "receipt.json", canonical({**record, "status": "complete"}))
        os.unlink("pending.json", dir_fd=directory)
        os.fsync(directory)
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent_fd)
    return str(target)
