"""Public plain-data contracts for controlled unaccepted draft admission."""

from __future__ import annotations

from ._facades._base import _process_exit, _translated
from .errors import UsageError
from .pack_discovery import owner

PROPOSAL = "evidence-draft-proposal/v1"
RESULT = "evidence-draft-admission/v1"


def _call(function, *args):
    name = args[0]
    if type(name) is not str or name not in {PROPOSAL, RESULT}:
        raise UsageError("DRAFT_ADMISSION_INVALID", "The draft schema is unavailable.", recoverable=False,
                         details={"reason": "schema_unknown"}, remediation="Select a published draft schema.") from None
    try:
        return getattr(owner("_draft_admission_contract"), function)(*args)
    except SystemExit as error:
        raise _process_exit(error) from None
    except Exception as error:
        try:
            translated = _translated(error)
        except SystemExit as renderer_exit:
            raise _process_exit(renderer_exit) from None
        except Exception:
            translated = None
        if translated is not None:
            raise translated from None
        raise UsageError("DRAFT_ADMISSION_INVALID", "The draft proposal cannot be admitted.", recoverable=False,
            details={"reason": "document_invalid"}, remediation="Use the current bounded draft contract and exact original/source bindings.") from None


def schema_document(name):
    return _call("schema_document", name)


def validate_document(name, value):
    return _call("validate_document", name, value)


def encode_document(name, value):
    return _call("encode_document", name, value)


def decode_document(name, raw):
    return _call("decode_document", name, raw)
