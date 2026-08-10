"""``ws.coverage`` -- coverage manifests and their evaluation.

One operation, ``evaluate``, on top of the same evaluator ``coverage_manifest.py``
drives for the CLI, so a host reads the identical report in-process. Add
operations here rather than in a shared module: sibling units own sibling
namespaces.
"""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class CoverageNamespace(Namespace):
    """Coverage operations for the owning workspace."""

    def evaluate(self, slug: str) -> dict[str, Any]:
        """Re-evaluate the coverage manifest for ``slug`` and return its report.

        Returns exactly the document ``coverage_manifest.py evaluate --format json``
        prints. Evaluating is not a read: the recomputed facet verdicts, coverage
        verdict and ``updated_at`` are written back to the manifest, so calling
        this leaves the same workspace state the CLI would have left.

        Raises:
            CoverageError: the manifest is missing, invalid, or its facets do not
                agree with the configured scope.
            QuestionError: ``slug`` is not a usable question slug.
            ConfigError: ``research.yml`` cannot be read, or the handle is closed.
        """
        return self._call("coverage_manifest", "run_evaluate", self._root, slug=slug)
