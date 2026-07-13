"""Deterministic one-shot fault injection for workspace mutation tests.

The harness deliberately patches a named mutation seam rather than relying on
filesystem permissions or a full disk.  That keeps quota/read-only and crash
tests deterministic on Windows, macOS, and Linux while still exercising the
production exception and recovery paths around the actual write boundary.
"""

from __future__ import annotations

import errno
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from unittest import mock

FAULT_POINTS = frozenset(
    {
        "before_temp_write",
        "after_temp_write",
        "before_replace",
        "after_replace",
        "during_event_append",
        "during_upgrade",
    }
)

FAULT_ERRNOS = {
    "read_only": errno.EACCES,
    "quota_exhausted": errno.ENOSPC,
    "transient_io": errno.EAGAIN,
}


class InjectedMutationFault(OSError):
    """An expected deterministic fault carrying stable machine taxonomy."""

    error_code = "MUTATION_FAULT_INJECTED"

    def __init__(self, fault_point: str, fault_kind: str = "transient_io") -> None:
        if fault_point not in FAULT_POINTS:
            raise ValueError(f"unknown mutation fault point: {fault_point}")
        if fault_kind not in FAULT_ERRNOS:
            raise ValueError(f"unknown mutation fault kind: {fault_kind}")
        self.fault_point = fault_point
        self.fault_kind = fault_kind
        super().__init__(FAULT_ERRNOS[fault_kind], f"injected {fault_kind} at {fault_point}")


@contextmanager
def inject_once(
    owner: Any,
    attribute: str,
    *,
    fault_point: str,
    timing: str,
    matches: Callable[..., bool] | None = None,
    fault_kind: str = "transient_io",
):
    """Raise once before or after a matching call to ``owner.attribute``.

    ``matches`` receives the original positional and keyword arguments.  A
    non-matching call is passed through, which lets tests target only the
    generated temp path while leaving raw evidence and user files untouched.
    """

    if timing not in {"before", "after"}:
        raise ValueError(f"unknown injection timing: {timing}")
    original = getattr(owner, attribute)
    injected = False

    def wrapped(*args: Any, **kwargs: Any):
        nonlocal injected
        selected = not injected and (matches is None or matches(*args, **kwargs))
        if selected and timing == "before":
            injected = True
            raise InjectedMutationFault(fault_point, fault_kind)
        result = original(*args, **kwargs)
        if selected and timing == "after":
            injected = True
            raise InjectedMutationFault(fault_point, fault_kind)
        return result

    with mock.patch.object(owner, attribute, new=wrapped):
        yield


def generated_temp(path: Any, *_args: Any, **_kwargs: Any) -> bool:
    """Return whether a path is one of the reserved generated temp artifacts."""

    name = getattr(path, "name", "")
    return isinstance(name, str) and (name.endswith(".tmp") or ".tmp." in name)
