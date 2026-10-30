"""Plain-data read-only diagnostics for unaccepted draft proposals."""

from ._facades._base import _process_exit, _translated
from .errors import UsageError
from .pack_discovery import owner


def schema_document():
    return owner("_claim_diagnostics").schema_document()


def current(root, proposal):
    """Run the canonical native mechanical checks without publication."""
    try:
        return owner("_claim_diagnostics").current(root, proposal)
    except SystemExit as error:
        raise _process_exit(error) from None
    except Exception as error:
        translated = _translated(error)
        if translated is not None:
            raise translated from None
        raise UsageError("CLAIM_DIAGNOSTICS_REFUSED", "Claim diagnostics are unavailable.",
            details={"reason":"diagnostic_input_invalid_or_unreadable"}) from None
