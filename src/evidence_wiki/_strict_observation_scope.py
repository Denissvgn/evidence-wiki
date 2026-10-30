"""Expiring native observations shared only with their owning publication read."""

from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy

from .pack_discovery import owner
from .research_session_contracts import refuse

_ACTIVE = ContextVar("native_strict_observation", default=None)
_MINT = object()


class _Observation:
    def __init__(self, holder, config, view, mint):
        from ._review_dependency_inputs import _task, assert_current

        if mint is not _MINT:
            refuse("STRICT_OBSERVATION_UNAVAILABLE")
        assert_current(holder)
        self.holder, self.root, self.view = holder, holder._root, view
        self.config = deepcopy(config)
        self.capture = owner("_strict_evidence").capture_workspace(self.root)
        self.files = dict(self.capture.files)
        self.read = owner("_publication_context").live_read.get()
        self.state, self.state_values = view.state, deepcopy(vars(view.state))
        self.trust = deepcopy(view.trust)
        self.thread, self.task = threading.get_ident(), _task()
        self.valid, self.token, self.observed = True, None, None

    def check(self, root, config, view):
        from ._review_dependency_inputs import _task

        try:
            if (not self.valid or _ACTIVE.get() is not self or root != self.root
                    or config != self.config or view is not self.view
                    or threading.get_ident() != self.thread or _task() is not self.task
                    or self.read is None or not self.read.active
                    or owner("_publication_context").live_read.get() is not self.read
                    or self.read.view is not view or view.state is not self.state
                    or vars(view.state) != self.state_values or view.trust != self.trust
                    or self.capture.files != self.files):
                refuse("STRICT_OBSERVATION_CHANGED")
            # Context tokens, unlike task/thread identity, reject copy_context().
            _ACTIVE.reset(self.token)
            self.token = _ACTIVE.set(self)
            self.holder._scope(root, config, self.capture, view)
            self.read.require(root, config)
        except BaseException:
            self.valid = False
            raise

    def constructor(self, root, config, capture):
        self.check(root, config, self.view)
        if capture.revision_id != self.capture.revision_id or capture.files != self.files:
            self.valid = False
            refuse("STRICT_OBSERVATION_CHANGED")
        return nullcontext()

    def register(self, root, config, policy, view, result, observed):
        self.check(root, config, view)
        capture, producer, currents, reviews = observed
        if capture.revision_id != self.capture.revision_id or capture.files != self.files:
            self.valid = False
            refuse("STRICT_OBSERVATION_CHANGED")
        if self.observed is not None:
            # Only the first actual evaluation is eligible for this operation.
            return
        self.policy = deepcopy(policy)
        self.result = deepcopy(result)
        # The actual producer generation is a frozen native capability holding
        # module identities; copying it would destroy its custody (and modules
        # are deliberately not serializable). Its public document is bytes-led.
        copied_currents, copied_reviews = deepcopy((currents, reviews))
        self.observed = capture, producer, copied_currents, copied_reviews
        self.check(root, config, view)

    def close(self):
        try:
            self.check(self.root, self.config, self.view)
            strict = owner("_strict_evidence")
            if strict.capture_workspace(self.root).revision_id != self.capture.revision_id:
                refuse("STRICT_OBSERVATION_CHANGED")
            if self.observed is not None:
                capture, producer, currents, reviews = self.observed
                strict._dependency_closing(self.root, self.config, self.policy,
                    self.view, capture, self.result["basis_id"], producer, currents, reviews)
                self.check(self.root, self.config, self.view)
        except BaseException:
            self.valid = False
            raise

    def publication(self, root, config, policy, view, assurance):
        self.check(root, config, view)
        if self.observed is None:
            return None
        if policy != self.policy or assurance != self.result["assurance"]:
            self.valid = False
            refuse("STRICT_OBSERVATION_CHANGED")
        self.close()
        return deepcopy(self.result)


def current(holder):
    scope = _ACTIVE.get()
    if scope is not None:
        if scope.holder is not holder:
            refuse("STRICT_OBSERVATION_CHANGED")
        scope.check(scope.root, scope.config, scope.view)
    return scope


def invalidate():
    scope = _ACTIVE.get()
    if scope is not None:
        scope.valid = False


@contextmanager
def observe(holder):
    """Hold the actual read lock through strict selection and publication."""
    strict = owner("_strict_evidence")
    config = strict.configuration(holder._root)
    policy = strict.resolve_policy(holder._root, config)
    usage = owner("_evidence_usage")
    if (policy is None or policy["schema_version"] != strict.POLICY_SCHEMA_V2
            or not owner("_caller_context").builtin_integrations(config)
            or not usage.configured(config)):
        yield
        return
    if _ACTIVE.get() is not None:
        refuse("STRICT_OBSERVATION_NESTED")
    with usage._observation_reads(holder._root, config):
        if owner("_publication_context").live_read.get() is None:
            # The native owner deliberately permits ordinary partial reads if
            # opening its view fails or an authorized capture already owns it.
            # Such reads cannot mint an observation proof or retry that open.
            yield
            return
        with usage.current_view(holder._root, config) as view:
            scope = _Observation(holder, config, view, _MINT)
            scope.token = _ACTIVE.set(scope)
            try:
                scope.check(scope.root, config, view)
                yield
                scope.close()
                from ._current_fact_observation import offer
                offer(scope)
            finally:
                scope.valid = False
                scope.observed = None
                _ACTIVE.reset(scope.token)
