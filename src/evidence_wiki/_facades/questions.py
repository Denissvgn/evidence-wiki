"""``ws.questions`` -- questions, their claims, and their answer pages.

The whole question lifecycle a host drives: intake a batch, claim it, resolve it
one of five ways, reopen it, and record the reviews that gate publication. Each
method is a one-line forward to the matching ``run_<op>`` seam in
``question_claim.py``, ``question_resolve.py`` or ``intake_questions.py``, and
every keyword mirrors the seam's -- which mirrors the CLI flag -- one for one. No
friendlier names and no convenience transformations: an operation that reads the
same on both doors is an operation that is hard to drift between them.

Hard, not impossible, and the difference is worth stating because it has already
happened: ``--require-decisive-scope`` was added to the parser, to
``dispatch_seam`` and to ``run_reopen``, and this file was not touched, so the CLI
refused an undecided pairing while an in-process host could not ask for that
behaviour at all. Mirroring by hand is a convention, and a convention holds until
someone edits three of the four places. So it is pinned by
``test_library_facade_forwards_every_seam_keyword``, which checks both halves --
that every seam keyword is reachable from the method, and that every keyword the
method accepts is actually passed on -- rather than by remembering this paragraph.

The audit trail is not optional and not the caller's job. Every mutating seam
below writes its ``log.md`` entry and its page frontmatter *inside* the call, in
the position the CLI has always written them, so a question moved in-process
leaves the same trail as one moved from a shell. Add operations here rather than
in a shared module: sibling units own sibling namespaces.
"""

from __future__ import annotations

from typing import Any

from ._base import Namespace

#: Scripts this namespace forwards to.
_CLAIM = "question_claim"
_RESOLVE = "question_resolve"
_INTAKE = "intake_questions"


class QuestionsNamespace(Namespace):
    """Question-lifecycle operations for the owning workspace.

    Raises, across every method below:
        ClaimError: the claim is held by another agent (``CLAIM_HELD``), is not
            stale enough to steal, or the question is not claimed by the caller.
        QuestionError: the slug is unknown, or the page or answer is unusable.
        LockError: the workspace write lock is held by another process.
        ConfigError: the workspace cannot be read, or the handle is closed.
    """

    # -- claim lifecycle ------------------------------------------------

    def claim(
        self,
        *,
        slug: str,
        agent_id: str,
        steal: bool = False,
        if_older_than: float | None = None,
    ) -> dict[str, Any]:
        """Claim a question (``open`` -> ``in_progress``).

        ``steal`` takes a claim another agent holds, bounded by ``if_older_than``
        to claims older than that many hours. The two require each other: stealing
        is never automatic and never unbounded, on this door as on the CLI.
        """
        return self._call(
            _CLAIM,
            "run_claim",
            self._root,
            slug=slug,
            agent_id=agent_id,
            steal=steal,
            if_older_than=if_older_than,
        )

    def release(self, *, slug: str, agent_id: str) -> dict[str, Any]:
        """Release a claim (``in_progress`` -> ``open``)."""
        return self._call(_CLAIM, "run_release", self._root, slug=slug, agent_id=agent_id)

    # -- resolutions ----------------------------------------------------

    def answer(
        self,
        *,
        slug: str,
        agent_id: str,
        answer_page: str,
        source_id: list[str] | None = None,
        allow_uncited: bool = False,
        allow_unclaimed: bool = False,
        confidence: str | None = None,
        evidence_strength: str | None = None,
        require_coverage: bool = False,
        require_grounding: bool = False,
        coverage_manifest: str | None = None,
        grounding_file: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a question as answered."""
        return self._call(
            _RESOLVE,
            "run_answer",
            self._root,
            slug=slug,
            agent_id=agent_id,
            answer_page=answer_page,
            source_id=source_id,
            allow_uncited=allow_uncited,
            allow_unclaimed=allow_unclaimed,
            confidence=confidence,
            evidence_strength=evidence_strength,
            require_coverage=require_coverage,
            require_grounding=require_grounding,
            coverage_manifest=coverage_manifest,
            grounding_file=grounding_file,
        )

    def block(
        self,
        *,
        slug: str,
        agent_id: str,
        blocked_reason: str,
        request_id: list[str] | None = None,
        allow_unclaimed: bool = False,
    ) -> dict[str, Any]:
        """Resolve a question as blocked on missing evidence."""
        return self._call(
            _RESOLVE,
            "run_block",
            self._root,
            slug=slug,
            agent_id=agent_id,
            blocked_reason=blocked_reason,
            request_id=request_id,
            allow_unclaimed=allow_unclaimed,
        )

    def defer(self, *, slug: str, agent_id: str, reason: str, allow_unclaimed: bool = False) -> dict[str, Any]:
        """Resolve a question as deferred."""
        return self._call(
            _RESOLVE,
            "run_defer",
            self._root,
            slug=slug,
            agent_id=agent_id,
            reason=reason,
            allow_unclaimed=allow_unclaimed,
        )

    def reject(self, *, slug: str, agent_id: str, reason: str, allow_unclaimed: bool = False) -> dict[str, Any]:
        """Resolve a question as rejected."""
        return self._call(
            _RESOLVE,
            "run_reject",
            self._root,
            slug=slug,
            agent_id=agent_id,
            reason=reason,
            allow_unclaimed=allow_unclaimed,
        )

    def reopen(
        self,
        *,
        slug: str,
        agent_id: str,
        source_id: list[str],
        request_id: list[str] | None = None,
        require_decisive_scope: bool = False,
    ) -> dict[str, Any]:
        """Move a blocked question back to open once its evidence is delivered and normalized.

        There is no ``allow_unclaimed`` here, matching the seam and the CLI: a
        blocked question is never claimed.

        ``require_decisive_scope`` refuses a pairing the declared scope did not
        determine, rather than reporting it as a warning.
        """
        return self._call(
            _RESOLVE,
            "run_reopen",
            self._root,
            slug=slug,
            agent_id=agent_id,
            source_id=source_id,
            request_id=request_id,
            require_decisive_scope=require_decisive_scope,
        )

    # -- review ---------------------------------------------------------

    def approve(self, *, slug: str, reviewer: str) -> dict[str, Any]:
        """Approve every policy still pending human review, in one call.

        The recorded principal is ``reviewer`` rather than an agent id, matching
        the CLI's trust model: this authenticates nobody, and the audit trail is
        the frontmatter entry plus ``log.md``.
        """
        return self._call(_RESOLVE, "run_approve", self._root, slug=slug, reviewer=reviewer)

    def review(
        self,
        *,
        slug: str,
        policy: str,
        verdict: str,
        reviewed_by: str,
        review_ref: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record one per-policy human review, collected inside or outside the workspace."""
        return self._call(
            _RESOLVE,
            "run_review",
            self._root,
            slug=slug,
            policy=policy,
            verdict=verdict,
            reviewed_by=reviewed_by,
            review_ref=review_ref,
            note=note,
        )

    # -- grounding ------------------------------------------------------

    def set_grounding(
        self,
        *,
        slug: str,
        agent_id: str,
        from_file: str,
        allow_unclaimed: bool = False,
    ) -> dict[str, Any]:
        """Replace a question's whole grounding block from a file, without resolving it.

        Named for the published surface (``questions.set_grounding``); the seam it
        forwards to is ``question_resolve.run_grounding_set``.
        """
        return self._call(
            _RESOLVE,
            "run_grounding_set",
            self._root,
            slug=slug,
            agent_id=agent_id,
            from_file=from_file,
            allow_unclaimed=allow_unclaimed,
        )

    # -- intake ---------------------------------------------------------

    def add_batch(self, *, from_file: str = "-", dry_run: bool = False) -> dict[str, Any]:
        """Inject a validated question batch, creating one page per question.

        ``from_file`` mirrors ``--from-file``, ``"-"`` included: that reads the
        batch from this process's stdin, exactly as the CLI does. ``dry_run``
        returns the report the writes *would* have produced without performing
        them -- the supported way to preview intake, since the page writes, the
        ``index.md`` update and the ``log.md`` entry are otherwise part of the
        same operation as the report.

        Raises:
            IntakeError: a cap, a rate limit, a field length, or a handoff
                signature check refused the batch.
        """
        return self._call(_INTAKE, "run_intake", self._root, from_file=from_file, dry_run=dry_run)
