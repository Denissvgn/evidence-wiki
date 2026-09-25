"""Portable access to the shared, model-free computation owner."""

from __future__ import annotations

from ._script_host import load_packaged_script, shared_assets_root
from .errors import ConfigError, error_from_envelope


def execute(project_root, operation="check", **options):
    """Evaluate read-only by default; mutation operations require result/request IDs."""
    service = load_packaged_script(shared_assets_root(), "_computation_service")
    try:
        return service.run(project_root, operation, **options)
    except Exception as error:
        if callable(getattr(error, "to_envelope", None)):
            raise error_from_envelope(error.to_envelope()) from None
        if isinstance(error, TypeError):
            raise ConfigError("COMPUTATION_REFUSED", "Computation options are invalid.", recoverable=False,
                              details={"reason": "computation_option_invalid"}) from None
        raise


def evaluate(project_root, *, as_of=None):
    return execute(project_root, as_of=as_of)


def schema_document(resource_id):
    documents = execute(None, "schemas")
    if not isinstance(resource_id, str) or resource_id not in documents:
        raise ConfigError("COMPUTATION_REFUSED", "Unknown computation schema.", recoverable=False,
                          details={"reason": "computation_schema_unknown"})
    return documents[resource_id]
