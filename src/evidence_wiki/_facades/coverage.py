"""``ws.coverage`` -- coverage manifests and their evaluation.

Skeleton only. **Filled by U10**, which adds ``evaluate(slug)`` on top of the
same evaluator the CLI drives, so a host reads the identical report in-process.
Add methods here rather than in a shared module: U10 works alongside units that
own sibling namespaces.
"""

from __future__ import annotations

from ._base import Namespace


class CoverageNamespace(Namespace):
    """Coverage operations for the owning workspace. No operations yet."""
