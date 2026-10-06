"""Closed current brief and deterministic artifact shapes confer no authority."""

from copy import deepcopy

from ._pack_io import bounded, canonical
from .errors import EvidenceWikiError, UsageError
from .mutation_contracts import detached
from .onboarding_contract import _matches
from .resolution_contracts import _schema_document as resolution_schema

BRIEF = "research-current-brief/v1"
MARKDOWN = "research-brief-markdown/v1"
MAX_BYTES = 1_048_576


def refuse(reason):
    raise UsageError("RESEARCH_BRIEF_REFUSED", "Current brief refused.",
        details={"reason": reason}, recoverable=False)


def _schemas(native=None):
    def obj(**fields):
        return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
    hashed = {"type": "string", "pattern": r"^sha256:[a-f0-9]{64}$(?![\s\S])"}
    fields = resolution_schema(native)["properties"]
    brief = obj(schema_version={"const": BRIEF}, resolution_basis_id=deepcopy(fields["current_basis_id"]),
        verdict=deepcopy(fields["verdict"]), original_outcomes=deepcopy(fields["original_outcomes"]),
        global_blockers=deepcopy(fields["global_blockers"]), accounting_complete={"type": "boolean"},
        limitations=deepcopy(fields["limitations"]), lifecycle=deepcopy(fields["lifecycle"]),
        readability=obj(state={"enum": ["not_requested", "pending", "observed", "refused"]},
            qualification={"enum": ["not_observed", "current_protected_purpose"]},
            scope={"enum": ["not_qualified", "finite_scalar_preservation"]},
            semantic_approval={"const": False}),
        authorized={"const": False}, external_action_authorized={"const": False})
    markdown = obj(schema_version={"const": MARKDOWN}, markdown={"type": "string", "maxLength": MAX_BYTES},
        markdown_sha256=hashed, brief_sha256=hashed, rendering={"const": "fixed_literal_projection"},
        authorized={"const": False}, external_action_authorized={"const": False})
    return {BRIEF: brief, MARKDOWN: markdown}


def schemas():
    return _schemas()


def checked(schema, value):
    try:
        bounded(value)
        _matches(value, schemas()[schema])
        if len(canonical(value)) > MAX_BYTES:
            refuse("document_bound")
        return detached(value)
    except UsageError as error:
        if error.error_code == "RESEARCH_BRIEF_REFUSED":
            raise
        refuse("document_invalid")
    except (EvidenceWikiError, KeyError, TypeError, ValueError, SystemExit):
        refuse("document_invalid")
