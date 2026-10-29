"""Private, operation-scoped access to actual immutable review configuration."""

from __future__ import annotations

import asyncio
import sys
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from pathlib import Path

from .mutation_contracts import detached
from .research_session_contracts import refuse

_ACTIVE = ContextVar("native_review_dependency_inputs", default=None)
_MINT = object()
_COUNTERS = ContextVar("native_review_dependency_counters", default=None)


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


class _Inputs:
    """A holder is minted from a genuine session, never from a public DTO."""

    def __init__(self, session, mint):
        from .host_context import current_context
        from .mutation_operations import _generation, _host_selection
        from .research_execution import ResearchExecution
        from .research_session import ResearchSession

        if mint is not _MINT or type(session) is not ResearchSession or type(session.execution) is not ResearchExecution:
            refuse("REVIEW_DEPENDENCY_INPUTS_UNAVAILABLE")
        if session.closed or current_context() is not session.context:
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        self._session = session
        self._execution = session.execution
        self._context = session.context
        self._root = session.root
        self._root_identity = self._identity()
        self._configuration_raw = self._configuration()
        self._host = _host_selection()
        self._generation = _generation()
        self._profile = session.execution.profile.document()
        self._policy = session.policy.to_dict()
        self._execution_identity = session.execution.identity()
        self._thread = threading.get_ident()
        self._task = _task()
        self._valid = True

    def counter_scope(self, root, config, capture, manifest, view):
        """Only the actual native evaluator can open shared observation custody."""
        from .pack_discovery import owner

        if sys._getframe(1).f_code is not owner("_strict_evidence")._evaluate_dependency_observations.__code__:
            refuse("REVIEW_DEPENDENCY_INPUTS_UNAVAILABLE")

        @contextmanager
        def scoped():
            """Share native question reads only during one actual claim evaluation."""
            self._scope(root, config, capture, view)
            if _COUNTERS.get() is not None:
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            scope = _CounterScope(self, root, config, capture, manifest, view, _MINT)
            scope._token = _COUNTERS.set(scope)
            try:
                scope.check(root, config, capture, manifest, view)
                yield
                scope.check(root, config, capture, manifest, view)
            finally:
                scope._valid = False
                scope.clear()
                _COUNTERS.reset(scope._token)

        return scoped()

    def observation_scope(self):
        from ._strict_observation_scope import observe

        self.assert_current()
        if getattr(self._session.execution.profile.review_delivery, "context_version", None) != "dependencies":
            return nullcontext()
        return observe(self)

    def observation_constructor(self, root, config, capture):
        from ._strict_observation_scope import current

        scope = current(self)
        return None if scope is None else scope.constructor(root, config, capture)

    def retain_strict_observation(self, root, config, policy, view, result, observed):
        from ._strict_observation_scope import current
        from .pack_discovery import owner

        self.assert_current()
        if sys._getframe(1).f_code is not owner("_strict_evidence")._evaluate_dependency_observations.__code__:
            refuse("STRICT_OBSERVATION_UNAVAILABLE")
        scope = current(self)
        if scope is not None:
            scope.register(root, config, policy, view, result, observed)

    def publication_observation(self, root, config, policy, view, assurance):
        from ._strict_observation_scope import current
        from .pack_discovery import owner

        self.assert_current()
        if sys._getframe(1).f_code is not owner("_strict_evidence")._publication_evaluation.__code__:
            refuse("STRICT_OBSERVATION_UNAVAILABLE")
        scope = current(self)
        return None if scope is None else scope.publication(root, config, policy, view, assurance)

    def application_observation(self, root, config, policy, view):
        from ._current_fact_observation import consume
        from .pack_discovery import owner

        # This must compare a plain helper, never a decorator wrapper code object.
        if sys._getframe(1).f_code is not owner("_strict_evidence")._application_observation.__code__:
            refuse("STRICT_OBSERVATION_UNAVAILABLE")
        return consume(self, root, config, policy, view)

    def _identity(self):
        info = self._root.stat()
        return (str(self._root), info.st_dev, info.st_ino)

    def _configuration(self):
        from ._filesystem import metadata_stat
        from ._pack_io import read_file, signature
        from .host_context import _root_descriptor

        path = self._root / "research.yml"
        self._context.require_path(path)
        observed = metadata_stat(path, follow_symlinks=False)
        with _root_descriptor(self._root) as descriptor:
            raw = read_file(descriptor, "research.yml", expected=signature(observed))
        if signature(metadata_stat(path, follow_symlinks=False)) != signature(observed):
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        return raw

    def assert_current(self):
        from .host_context import current_context
        from .mutation_operations import _generation, _host_selection

        try:
            session = self._session
            if (not self._valid or _ACTIVE.get() is not self or threading.get_ident() != self._thread
                    or _task() is not self._task or session.closed or session.execution is not self._execution
                    or session.context is not self._context or current_context() is not self._context
                    or session.root != self._root or self._identity() != self._root_identity
                    or self._configuration() != self._configuration_raw
                    or _host_selection() != self._host or _generation() != self._generation
                    or session.execution.profile.document() != self._profile
                    or session.policy.to_dict() != self._policy
                    or session.execution.identity() != self._execution_identity):
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            if session._owner_workspace is not None:
                session._owner_workspace._check_open()
            self._context.require_path(self._root)
        except BaseException:
            self._valid = False
            raise

    def public_profile(self):
        self.assert_current()
        return detached({"execution_profile": self._profile, "run_policy": self._policy})

    def role_semantics(self):
        from .execution_session import _review_role_semantics

        self.assert_current()
        result = _review_role_semantics(self._session)
        self.assert_current()
        return result

    def _scope(self, root, config, capture, view):
        from ._pack_io import yaml_document

        self.assert_current()
        try:
            if (Path(root).resolve(strict=True) != self._root or capture.files["research.yml"] != self._configuration_raw
                    or yaml_document(capture.files["research.yml"]) != config):
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            if view is None:
                refuse("REVIEW_DEPENDENCY_USAGE_REQUIRED")
            view.revalidate(self._root, config)
        except BaseException:
            self._valid = False
            raise

    def source_documents(self, root, config, capture, view):
        """Read full permitted source records using the existing native owner."""
        from .pack_discovery import owner
        from .retrieval_operations import _documents, _ScopedView

        scope = _COUNTERS.get()
        try:
            self._scope(root, config, capture, view)
        except BaseException:
            if scope is not None:
                scope.clear()
            raise
        publication = owner("_publication_context")
        if publication.capture_active() and publication.captured_view(root, config) is not view:
            if scope is not None:
                scope.clear()
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        if scope is not None:
            scope.check(root, config, capture, scope._manifest, view)
            if scope._holder is not self:
                scope.clear()
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            if scope._source_documents is not None:
                try:
                    source_view = scope._source_view
                    source_view.recheck_scope()
                    source_view.finish()
                    revisions = owner("_publication_usage").qualify_capture(capture.files, config, view,
                        purpose="qa-export", consumer="evidence-wiki")["selected"]
                    if revisions != {row["source_id"]: row["source_revision"] for row in scope._source_documents}:
                        refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
                    self._scope(root, config, capture, view)
                    source_view.recheck_scope()
                    source_view.finish()
                    scope.check(root, config, capture, scope._manifest, view)
                    return deepcopy(scope._source_documents)
                except BaseException:
                    scope.clear()
                    raise
        source_view = _ScopedView(self._root)
        selected = sorted(source_view.manifest(), key=lambda value: value.encode("utf-8"))
        if len(selected) > 32:
            refuse("REVIEW_DEPENDENCY_SOURCE_BOUND")
        if publication.capture_active():
            inspection_scope = nullcontext()
        else:
            inspection_scope = publication.authorized_capture(root, config, view)
        with inspection_scope:
            _, facts, unprocessed, unknown = _documents(source_view, selected, usage_view=view)
        if unprocessed or unknown or {row["source_id"] for row in facts.values()} != set(selected):
            refuse("REVIEW_DEPENDENCY_SOURCE_UNAVAILABLE")
        revisions = owner("_publication_usage").qualify_capture(capture.files, config, view,
            purpose="qa-export", consumer="evidence-wiki")["selected"]
        result = []
        for fact in facts.values():
            raw = source_view.files[fact["document"].path]
            if capture.files.get(fact["document"].path) != raw:
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            metadata, body, error = owner("_normalized_contract").split_record(raw.decode("utf-8"))
            if error or fact["source_id"] not in revisions:
                refuse("REVIEW_DEPENDENCY_SOURCE_UNAVAILABLE")
            result.append({"source_id": fact["source_id"], "source_revision": revisions[fact["source_id"]],
                "record_sha256": fact["normalized_sha256"], "metadata": metadata, "content": body})
        source_view.recheck_scope()
        source_view.finish()
        self._scope(root, config, capture, view)
        result = sorted(result, key=lambda row: row["source_id"].encode("utf-8"))
        if scope is not None:
            scope.check(root, config, capture, scope._manifest, view)
            scope._source_documents = deepcopy(result)
            scope._source_view = source_view
            return deepcopy(scope._source_documents)
        return result

    def counter_projection(self, root, config, capture, manifest, closure_claim_ids, question_slugs, view):
        """Preserve the existing native counter projection in original coordinates."""
        from .coverage_examination import check_instruction
        from .coverage_observation import observe_questions
        from .review_context import _metadata

        self._scope(root, config, capture, view)
        sources = self.source_documents(root, config, capture, view)
        by_source = {row["source_id"]: row for row in sources}
        claims = {row["id"]: row for row in manifest["claims"]}
        if (type(closure_claim_ids) is not list or len(closure_claim_ids) > 32
                or len(set(closure_claim_ids)) != len(closure_claim_ids)
                or any(identifier not in claims for identifier in closure_claim_ids)):
            refuse("REVIEW_DEPENDENCY_CLOSURE_INVALID")
        expected_questions = sorted({claims[identifier]["question_slug"] for identifier in closure_claim_ids},
            key=lambda value: value.encode("utf-8"))
        if list(question_slugs) != expected_questions:
            refuse("REVIEW_DEPENDENCY_QUESTION_SCOPE_CHANGED")
        primary = {item["source_id"] for identifier in closure_claim_ids for item in claims[identifier]["evidence"]}
        if config.get("computation") is not None:
            from .pack_discovery import owner
            computation = owner("_computation_service").required(self._root, config, view=view)
            primary.update(row["source_id"] for row in computation["sources"])
        fields = {"schema_version", "question_id", "question_slug", "original_ids", "facets", "criteria", "scope",
            "claims", "anchors", "source_contexts", "semantic_approval", "accepted", "write_authority", "basis_id", "owner_observation"}
        questions = []
        scope = _COUNTERS.get()
        if scope is None:
            instructions = observe_questions(self._root, question_slugs=expected_questions, source_ids=list(by_source))
        else:
            instructions = scope.instructions(self, root, config, capture, manifest, view, sources, expected_questions)
        for slug, observed in zip(expected_questions, instructions, strict=True):
            instruction = check_instruction(observed)
            if set(instruction) != fields:
                refuse("REVIEW_DEPENDENCY_COUNTER_UNSUPPORTED")
            own = [claims[identifier] for identifier in closure_claim_ids if claims[identifier]["question_slug"] == slug]
            instructed = {row["id"]: row for row in instruction["claims"]}
            if any(instructed.get(row["id"]) != row for row in own):
                refuse("REVIEW_DEPENDENCY_COUNTER_CLAIM_CHANGED")
            retained = {row["source_id"]: row for row in instruction["source_contexts"]}
            for sid, source in by_source.items():
                actual = retained.get(sid)
                if actual is None or actual["record_sha256"] != source["record_sha256"] or actual["text"] != source["content"]:
                    refuse("REVIEW_DEPENDENCY_COUNTER_SOURCE_CHANGED")
            questions.append({"question_slug": slug, "claim_ids": [row["id"] for row in own],
                "scope": detached(instruction["scope"]), "criteria": detached(instruction["criteria"]),
                "alternative_sources": [{"source_id": sid, "record_sha256": by_source[sid]["record_sha256"],
                    "metadata": _metadata(by_source[sid]["metadata"]), "content": by_source[sid]["content"]}
                    for sid in sorted(set(retained) - primary, key=lambda value: value.encode("utf-8"))]})
        self._scope(root, config, capture, view)
        return {"state": "current_native_instruction", "questions": questions, "slices": [],
            "receipt_semantics": [], "search_completeness": "not_established", "limitations": []}


class _CounterScope:
    """An expiring owner capability; copied contexts cannot consume its reads."""

    def __init__(self, holder, root, config, capture, manifest, view, mint):
        if mint is not _MINT or type(holder) is not _Inputs:
            refuse("REVIEW_DEPENDENCY_INPUTS_UNAVAILABLE")
        self._holder, self._root = holder, root
        self._config, self._capture, self._manifest, self._view = config, capture, manifest, view
        self._configuration, self._files, self._inventory = deepcopy(config), dict(capture.files), deepcopy(manifest)
        self._state, self._checkpoint = view.state, view.state.checkpoint
        self._state_values = deepcopy(vars(view.state))
        self._trust = deepcopy(view.trust)
        self._thread, self._task = threading.get_ident(), _task()
        self._valid, self._token = True, None
        self._sources, self._instructions = None, {}
        self._prefetch = True
        self._source_documents, self._source_view = None, None

    def clear(self):
        self._instructions.clear()
        self._source_documents, self._source_view = None, None

    def check(self, root, config, capture, manifest, view):
        try:
            if (not self._valid or _COUNTERS.get() is not self or root != self._root
                    or config is not self._config or capture is not self._capture
                    or manifest is not self._manifest or view is not self._view
                    or threading.get_ident() != self._thread or _task() is not self._task
                    or config != self._configuration or capture.files != self._files
                    or manifest != self._inventory or view.state is not self._state
                    or view.state.checkpoint != self._checkpoint or vars(view.state) != self._state_values
                    or view.trust != self._trust):
                refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
            # Tokens are tied to their originating Context, including on one thread.
            _COUNTERS.reset(self._token)
            self._token = _COUNTERS.set(self)
            self._holder._scope(root, config, capture, view)
        except BaseException:
            self._valid = False
            self.clear()
            raise

    def instructions(self, holder, root, config, capture, manifest, view, sources, slugs):
        from .coverage_observation import observe_questions
        from .errors import EvidenceWikiError

        self.check(root, config, capture, manifest, view)
        if holder is not self._holder or self._sources is not None and sources != self._sources:
            self._valid = False
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        if self._sources is None:
            self._sources = deepcopy(sources)
        requested = sorted({row["question_slug"] for row in manifest["claims"]}, key=lambda value: value.encode("utf-8"))
        if len(requested) > 32 or not self._prefetch:
            requested = slugs
        missing = [slug for slug in requested if slug not in self._instructions]
        if missing:
            selected = [row["source_id"] for row in sources]
            try:
                observed = observe_questions(self._holder._root, question_slugs=missing, source_ids=selected)
            except (EvidenceWikiError, ValueError) as exc:
                # A different question's invalid anchor must not widen this closure's failure.
                # Only the native coverage codec's inert refusals permit speculative fallback.
                if (not set(missing) - set(slugs)
                        or isinstance(exc, EvidenceWikiError) and (exc.error_code != "COVERAGE_EXAMINATION_INVALID"
                            or exc.details.get("reason") == "native_observation_changed")):
                    raise
                self.check(root, config, capture, manifest, view)
                if sources != self._sources:
                    self._valid = False
                    refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
                self._prefetch = False
                missing = [slug for slug in slugs if slug not in self._instructions]
                observed = observe_questions(self._holder._root, question_slugs=missing, source_ids=selected)
            self.check(root, config, capture, manifest, view)
            self._instructions.update(zip(missing, deepcopy(observed), strict=True))
        self.check(root, config, capture, manifest, view)
        return deepcopy([self._instructions[slug] for slug in slugs])


def current_inputs():
    holder = _ACTIVE.get()
    if holder is not None:
        assert_current(holder)
    return holder


def assert_current(holder):
    if type(holder) is not _Inputs:
        refuse("REVIEW_DEPENDENCY_INPUTS_UNAVAILABLE")
    holder.assert_current()


@contextmanager
def bind_session(session):
    """Nested calls reuse the active owner and never replace its authority."""
    if session.execution is None:
        if _ACTIVE.get() is not None:
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        yield None
        return
    holder = _ACTIVE.get()
    if holder is not None:
        assert_current(holder)
        if holder._session is not session:
            refuse("REVIEW_DEPENDENCY_INPUTS_CHANGED")
        yield holder
        assert_current(holder)
        return
    holder = _Inputs(session, _MINT)
    token = _ACTIVE.set(holder)
    try:
        from .host_context import review_producer_binding
        selected = getattr(session.execution.profile.review_delivery, "context_version", None) == "dependencies"
        with review_producer_binding() if selected else nullcontext():
            assert_current(holder)
            yield holder
            assert_current(holder)
    finally:
        holder._valid = False
        _ACTIVE.reset(token)


@contextmanager
def bind_constructing_session(session):
    """Bind validated construction inputs before the first native current read."""
    with bind_session(session) as holder:
        yield holder


def bound_session(function):
    """Protect a direct internal/public owner entry for its full call interval."""
    @wraps(function)
    def guarded(session, *args, **kwargs):
        from .research_session import ResearchSession
        if type(session) is not ResearchSession:
            return function(session, *args, **kwargs)
        with bind_session(session):
            return function(session, *args, **kwargs)
    return guarded
