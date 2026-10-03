"""One host-owned deadline shared across canonical operation phases."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .errors import ConfigError
from .pack_discovery import owner


def _translate(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    from ._facades._base import _translated

    try:
        return call(*args, **kwargs)
    except (Exception, SystemExit) as error:
        translated = _translated(error)
        if translated is None:
            raise
        raise translated from None


class OperationControl:
    """Explicit trusted host cancellation and the original finite allowance.

    Cancellation callbacks are observed through one bounded host-only probe.
    Probe timeout preserves cancellation uncertainty and stops native work;
    arbitrary callback code itself is not claimed cancelled. The callback never
    enters an intent or extraction worker. Earlier publications can remain.
    """

    def __init__(self, *, deadline_utc: str, timeout_seconds: float,
                 cancel_requested: Callable[[], bool] | None = None) -> None:
        native = owner("_operation_control")
        if type(deadline_utc) is str:
            try:
                clock = datetime.fromisoformat(deadline_utc.replace("Z", "+00:00"))
                if clock.tzinfo is not None and clock.utcoffset().total_seconds() == 0:
                    deadline_utc = clock.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except ValueError:
                pass
        self._native = _translate(native.OperationControl, deadline_utc=deadline_utc,
                                  timeout_seconds=timeout_seconds, cancel_requested=cancel_requested)
        self._adapters: dict[type, Any] = {}

    @property
    def deadline_utc(self) -> str:
        return self._native.deadline_utc

    @property
    def timeout_seconds(self) -> float:
        return self._native.timeout_seconds

    def checkpoint(self, name: str) -> None:
        _translate(self._native.checkpoint, name)

    def remaining_seconds(self, name: str = "remaining") -> float:
        return _translate(self._native.remaining_seconds, name)

    def snapshot(self) -> dict[str, str | float]:
        return _translate(self._native.snapshot)

    def _for_owner(self, module: Any) -> Any:
        """Bridge isolated script class identity without creating a new clock."""
        native_class = module.OperationControl
        if native_class not in self._adapters:
            controller = self

            class BoundControl(native_class):
                def checkpoint(self, name):
                    controller._native.checkpoint(name)

                def remaining_seconds(self, name="remaining"):
                    return controller._native.remaining_seconds(name)

                def snapshot(self):
                    return controller._native.snapshot()

            adapter = object.__new__(BoundControl)
            adapter.deadline_utc = self.deadline_utc
            adapter.timeout_seconds = self.timeout_seconds
            self._adapters[native_class] = adapter
        return self._adapters[native_class]


def checkpoint(control: OperationControl | None, name: str) -> None:
    """Refuse unknown controls; check the exact public host-owned object."""
    if control is not None:
        if type(control) is not OperationControl:
            raise ConfigError("OPERATION_CONTROL_INVALID", "An explicit host operation control is required.",
                                    recoverable=False, details={"checkpoint": "type"})
        control.checkpoint(name)


def native_control(control: OperationControl | None, module: Any) -> Any:
    """Select the native-compatible view of the same original allowance."""
    if control is None:
        return None
    checkpoint(control, "before_native_operation")
    return control._for_owner(module)
