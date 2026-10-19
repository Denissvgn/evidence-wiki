"""Explicit immutable host delivery bounds never certify model comprehension."""

from dataclasses import dataclass

from evidence_wiki.errors import UsageError


@dataclass(frozen=True)
class ReviewDelivery:
    page_chars: int = 4096
    max_packet_bytes: int = 196608
    max_input_bytes: int = 524288
    context_version: str = "legacy"

    def __post_init__(self):
        if type(self.context_version) is not str or self.context_version not in {"legacy", "complete", "dependencies"}:
            raise UsageError("REVIEW_DELIVERY_INVALID", "The review context version is invalid.", details={"field": "context_version"})
        for name, lower, upper in (("page_chars", 256, 16384), ("max_packet_bytes", 4096, 196608),
                ("max_input_bytes", 4096, 524288)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise UsageError("REVIEW_DELIVERY_INVALID", "The complete review delivery bounds are invalid.", details={"field": name})
        if self.max_input_bytes < self.max_packet_bytes:
            raise UsageError("REVIEW_DELIVERY_INVALID", "The input must accommodate the complete packet.", details={"field": "max_input_bytes"})

    def document(self):
        result = {"schema_version": "research-review-delivery/v1", "mode": "bounded_host_assembled",
            "page_chars": self.page_chars, "max_packet_bytes": self.max_packet_bytes, "max_input_bytes": self.max_input_bytes,
            "required_sections": ["sources", "computation_if_configured", "mechanical", "counter_context"],
            "worker_comprehension": "not_observed", "semantic_approval": "not_evaluated"}
        if self.context_version == "complete":
            result.update(schema_version="research-review-delivery/v2", context_version="research-review-context/v3")
        elif self.context_version == "dependencies":
            result.update(schema_version="research-review-delivery/v3", context_version="research-review-context/v4")
        return result
