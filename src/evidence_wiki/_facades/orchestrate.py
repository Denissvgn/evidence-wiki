"""``ws.orchestrate`` -- orchestration sessions and their work orders.

Skeleton only. **Filled by U11**, which adds ``session(run_id)`` returning a
session object whose ``next()`` yields the next work order. Add methods here
rather than in a shared module: U11 works alongside units that own sibling
namespaces.
"""

from __future__ import annotations

from ._base import Namespace


class OrchestrateNamespace(Namespace):
    """Orchestration operations for the owning workspace. No operations yet."""
