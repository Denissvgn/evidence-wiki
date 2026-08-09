"""``ws.grounding`` -- quote containment and claim grounding.

Skeleton only. **Filled by U10**, which adds ``verify(slug)`` with the same
containment semantics the CLI path uses. Add methods here rather than in a
shared module: U10 works alongside units that own sibling namespaces.
"""

from __future__ import annotations

from ._base import Namespace


class GroundingNamespace(Namespace):
    """Grounding operations for the owning workspace. No operations yet."""
