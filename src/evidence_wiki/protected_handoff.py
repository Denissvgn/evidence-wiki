"""Immutable bundled-adapter contract for native protected input handoff."""

from dataclasses import dataclass

from .agent_execution import ExecutionError

HANDOFF = "research-native-input-handoff/v1"


@dataclass(frozen=True)
class NativeInputHandoff:
    """Declare a finite native stdin boundary; never a model/adapter ACK."""

    delivery_seconds: int = 30

    def __post_init__(self):
        if type(self.delivery_seconds) is not int or not 1 <= self.delivery_seconds <= 30:
            raise ExecutionError("protected_handoff_ceiling_invalid")

    def document(self):
        return {"schema_version": HANDOFF, "adapter": "exact_bundled_subprocess_executor",
            "route": "new_protected_scoped_repair_invocation",
            "boundary": "same_thread_native_usage_view_through_stdin", "delivery_seconds": self.delivery_seconds,
            "generic_adapters": "unsupported", "unknown_effects": "reservation_retained",
            "model_comprehension": "not_observed"}

    @classmethod
    def restore(cls, value):
        if type(value) is not dict or set(value) != set(cls().document()):
            raise ExecutionError("protected_handoff_document_invalid")
        selected = cls(value["delivery_seconds"])
        if selected.document() != value:
            raise ExecutionError("protected_handoff_document_invalid")
        return selected


def schema_document():
    properties = {key: {"const": value} for key, value in NativeInputHandoff().document().items()}
    properties["delivery_seconds"] = {"type": "integer", "minimum": 1, "maximum": 30}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
