"""Inert decoding of complete blinded review material for hosts and workers."""

from ._review_dependency_wire import WIRE, unpack


def decode_dependency_material(value):
    """Expand a wire child exactly; this grants no identity or approval authority.

    Plain material retains its ordinary execution budget. The parent must still
    reverse authenticated aliases and validate native semantic identities.
    """
    if type(value) is dict and value.get("schema_version") == WIRE:
        return unpack(value, "blinded_material")
    import json

    from .agent_execution import _json

    return json.loads(_json(value))
