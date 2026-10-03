"""Coverage authoring, revision migration and evaluation through their owners."""

from __future__ import annotations

from typing import Any

from ._base import Namespace


class CoverageNamespace(Namespace):
    """Coverage operations for the owning workspace."""

    def init(self, slug: str, *, template: dict[str, Any] | None = None,
             coverage_profile: str | None = None, force: bool = False) -> dict[str, Any]:
        """Initialize an inline coverage template without a caller-managed file."""
        return self._call("coverage_manifest", "run_init_inline", self._root, slug=slug,
                          template=template, coverage_profile=coverage_profile, force=force)

    def revision(self, request: dict[str, Any]) -> dict[str, Any]:
        """Apply the canonical revision owner's explicit migration request."""
        return self._call("coverage_manifest", "run_revision", self._root, request=request)

    def link(self, slug: str, facet_id: str, *, accepted_source_ids: list[str] | None = None,
             blocking_request_ids: list[str] | None = None, clear_accepted_source_ids: bool = False,
             clear_blocking_request_ids: bool = False) -> dict[str, Any]:
        """Link declared sources/requests while retaining scope conflict checks."""
        return self._call("coverage_manifest", "run_link", self._root, slug=slug, facet_id=facet_id,
            accepted_source_ids=accepted_source_ids, blocking_request_ids=blocking_request_ids,
            clear_accepted_source_ids=clear_accepted_source_ids, clear_blocking_request_ids=clear_blocking_request_ids)

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
        return self._call("coverage_manifest", "run_evaluate_checked", self._root, slug=slug)
