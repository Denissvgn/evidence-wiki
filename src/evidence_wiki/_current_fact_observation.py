"""Private continuation of an actual native observation within its owner pass."""

import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .pack_discovery import owner
from .research_session_contracts import refuse

_PENDING = ContextVar('constructing_current_fact_observation', default=None)
_ACTIVE = ContextVar('application_current_fact_observation', default=None)
_OWNER = ContextVar('current_fact_owner_context', default=None)


class _OwnerEpoch:
    """Bind one native current-facts pass to its original execution context."""

    def __init__(self, facts):
        self.facts, self.valid, self.token = facts, True, None
        self.sources = _CurrentSourceContext.create(facts)

    def check(self, facts):
        try:
            if not self.valid or _OWNER.get() is not self or self.facts is not facts:
                refuse('CURRENT_OWNER_EPOCH_CHANGED')
            # This token predates application activation and belongs to the
            # native owner's original Context, including before its first use.
            _OWNER.reset(self.token)
            self.token = _OWNER.set(self)
        except BaseException:
            self.valid = False
            invalidate(self.facts)
            raise


class _CurrentSourceContext:
    """Retain context identity, never retrieval results or source eligibility."""

    @classmethod
    def create(cls, facts):
        from ._review_dependency_inputs import current_inputs
        from .research_observation import ResearchObservation
        from .research_session import ResearchSession

        if facts.operation != 'current':
            return None
        holder = current_inputs()
        if holder is None or type(facts.session) is not ResearchSession:
            return None
        if type(facts.observed) is not ResearchObservation:
            return None
        return cls(facts, holder)

    def __init__(self, facts, holder):
        from ._review_dependency_inputs import _task

        self.facts, self.holder, self.observed = facts, holder, facts.observed
        self.capture = self.observed.capture
        self.files = dict(self.capture.files)
        self.config, self.requirements = deepcopy((self.observed.config, self.observed.requirements))
        self.producer = self.observed.basis['implementation']
        self.root, self.execution = facts.session.root, facts.session.execution
        self.thread, self.task = threading.get_ident(), _task()

    def check(self, session):
        from . import execution_session as native
        from ._review_dependency_inputs import _task, current_inputs
        from .research_observation import _default_run, _reading_scope, _require_interpreter, _requirements

        facts = self.facts
        if (type(facts) is not native._CurrentFacts or native._CURRENT_FACTS.get() is not facts
                or facts.operation != 'current' or facts.invalidated or session is not facts.session
                or facts.observed is not self.observed or self.observed.capture is not self.capture
                or self.capture.files != self.files or self.observed.config != self.config
                or self.observed.requirements != self.requirements or self.observed.root != self.root
                or session.root != self.root or session.execution is not self.execution
                or threading.get_ident() != self.thread or _task() is not self.task
                or current_inputs() is not self.holder or self.holder._session is not session):
            refuse('CURRENT_OWNER_EPOCH_CHANGED')
        _require_owner(facts)
        self.holder.assert_current()
        _require_interpreter()
        facts.require(session)
        qualified = owner('_caller_context').builtin_integrations(self.config)
        if qualified != self.observed.integrations_qualified:
            refuse('CURRENT_OWNER_EPOCH_CHANGED')
        with _reading_scope(self.observed):
            _default_run(self.root, qualified)
            if (_requirements(self.root, self.capture.files) != self.requirements
                    or owner('_selected_publication').producer_identity() != self.producer):
                refuse('CURRENT_OWNER_EPOCH_CHANGED')
            self.observed.finish()
        self.holder.assert_current()
        _require_interpreter()
        facts.require(session)


def source_scope(session):
    """Admit only the native source helper to the original owner context."""
    from . import execution_session as native

    if sys._getframe(1).f_code is not native._host_sources.__code__:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    return _source_scope(session)


@contextmanager
def _source_scope(session):
    from . import execution_session as native

    facts = native._CURRENT_FACTS.get()
    if facts is None:
        yield None
        return
    if type(facts) is not native._CurrentFacts:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    try:
        _require_owner(facts)
        source = _OWNER.get().sources
        if source is None:
            facts.require(session)
            yield None
            facts.require(session)
        else:
            source.check(session)
            yield source.observed
            source.check(session)
    except BaseException:
        facts.invalidated = True
        invalidate(facts)
        raise


def owner_epoch(facts):
    from .execution_session import _current_facts
    if sys._getframe(1).f_code is not _current_facts.__wrapped__.__code__:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    return _owner_epoch(facts)


@contextmanager
def _owner_epoch(facts):
    if _OWNER.get() is not None:
        refuse('CURRENT_OWNER_EPOCH_NESTED')
    epoch = _OwnerEpoch(facts)
    epoch.token = _OWNER.set(epoch)
    try:
        yield
    finally:
        epoch.valid = False
        _OWNER.reset(epoch.token)


def _require_owner(facts):
    epoch = _OWNER.get()
    if type(epoch) is not _OwnerEpoch:
        invalidate(facts)
        refuse('CURRENT_OWNER_EPOCH_CHANGED')
    epoch.check(facts)


def constructing(facts):
    from .execution_session import _CurrentFacts
    if sys._getframe(1).f_code is not _CurrentFacts.__init__.__code__:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    return _constructing(facts)


@contextmanager
def _constructing(facts):
    from .execution_session import _CurrentFacts
    if type(facts) is not _CurrentFacts or _PENDING.get() is not None:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    token = _PENDING.set(facts)
    try:
        yield
    finally:
        _PENDING.reset(token)


def offer(scope):
    from . import execution_session as native
    from ._strict_observation_scope import observe
    facts = _PENDING.get()
    # Custody is transferred only by the actual successful scope close while
    # the genuine constructor is synchronously awaiting its native observation.
    if sys._getframe(1).f_code is not observe.__wrapped__.__code__:
        refuse('STRICT_OBSERVATION_UNAVAILABLE')
    if facts is None or type(facts) is not native._CurrentFacts or facts.operation != 'advance':
        return
    if (scope.holder._session is not facts.session or scope.observed is None
            or not scope.valid or facts._review_observation is not None):
        return
    scope.check(scope.root, scope.config, scope.view)
    capture, producer, currents, reviews = scope.observed
    facts._review_observation = _Continuation(facts, scope, capture, producer, currents, reviews)


class _Continuation:
    def __init__(self, facts, scope, capture, producer, currents, reviews):
        from ._review_dependency_inputs import _task
        self.facts, self.holder = facts, scope.holder
        self.config, self.policy = deepcopy((scope.config, scope.policy))
        self.capture, self.producer = capture, producer
        self.files = dict(capture.files)
        self.result, self.currents, self.reviews = deepcopy((scope.result, currents, reviews))
        self.state = deepcopy(vars(scope.view.state))
        self.trust = deepcopy(scope.view.trust)
        self.thread, self.task = threading.get_ident(), _task()
        self.valid, self.token = True, None
        self.packets = None

    def check(self):
        from . import execution_session as native
        from ._review_dependency_inputs import _task, current_inputs
        facts = self.facts
        if (not self.valid or _ACTIVE.get() is not self or native._CURRENT_FACTS.get() is not facts
                or type(facts) is not native._CurrentFacts or facts.operation != 'advance'
                or facts.invalidated or facts._review_observation is not self
                or self.packets is None or facts.packets != self.packets
                or threading.get_ident() != self.thread or _task() is not self.task
                or current_inputs() is not self.holder or self.holder._session is not facts.session):
            self.valid = False
            refuse('CURRENT_OWNER_EPOCH_CHANGED')
        try:
            _require_owner(facts)
            _ACTIVE.reset(self.token)
            self.token = _ACTIVE.set(self)
            self.holder.assert_current()
            facts.require(facts.session)
        except BaseException:
            self.valid = False
            raise


@contextmanager
def application_scope(session, attempt, application):
    from . import execution_session as native
    facts = native._CURRENT_FACTS.get()
    continuation = None
    if type(facts) is native._CurrentFacts and facts.operation == 'advance':
        rows = native._attempt_rows(native.retained(session))
        row = rows.get(attempt.attempt_id)
        if row is not None and native._ordinary_contextual_review(session, row, attempt, application):
            continuation = facts._review_observation
    if continuation is None:
        yield
        return
    _require_owner(facts)
    if _ACTIVE.get() is not None:
        refuse('CURRENT_OWNER_EPOCH_NESTED')
    continuation.token = _ACTIVE.set(continuation)
    try:
        continuation.check()
        yield
        continuation.check()
    except BaseException:
        continuation.valid = False
        raise
    finally:
        _ACTIVE.reset(continuation.token)
        continuation.token = None


def consume(holder, root, config, policy, view):
    from ._review_dependency_inputs import _Inputs
    if sys._getframe(1).f_code is not _Inputs.application_observation.__code__:
        refuse('STRICT_OBSERVATION_UNAVAILABLE')
    continuation = _ACTIVE.get()
    if continuation is None:
        holder.assert_current()
        return None
    try:
        continuation.check()
        # Native strict calls use either private or public owner siblings;
        # current_view may also borrow the original observation owner's view.
        # Resolve actual namespaces on every use and retain exact class identity.
        view_types = (owner('_evidence_usage').UsageView,
            owner('_strict_evidence').sibling('_evidence_usage').UsageView,
            owner('strict_evidence').sibling('_evidence_usage').UsageView)
        if (holder is not continuation.holder or root != holder._root
                or config != continuation.config or policy != continuation.policy
                or continuation.capture.files != continuation.files
                or vars(view.state) != continuation.state or view.trust != continuation.trust
                or not any(type(view) is expected for expected in view_types)):
            refuse('CURRENT_OWNER_EPOCH_CHANGED')
        holder._scope(root, config, continuation.capture, view)
        qualified = owner('_publication_usage').qualify_capture(continuation.files,
            config, view, purpose='qa-export', consumer='evidence-wiki')
        if qualified.get('complete') is not True:
            refuse('CURRENT_OWNER_EPOCH_CHANGED')
        owner('_strict_evidence')._dependency_closing(root, config, policy, view,
            continuation.capture, continuation.result['basis_id'], continuation.producer,
            continuation.currents, continuation.reviews)
        continuation.check()
        currents, reviews, result = deepcopy((continuation.currents, continuation.reviews, continuation.result))
        return result, (continuation.capture, continuation.producer, currents, reviews)
    except BaseException:
        continuation.valid = False
        raise


def seal(facts):
    from .execution_session import _CurrentFacts
    if sys._getframe(1).f_code is not _CurrentFacts.__init__.__code__:
        refuse('CURRENT_OWNER_EPOCH_INVALID')
    continuation = facts._review_observation
    if continuation is not None:
        if continuation.facts is not facts or continuation.packets is not None:
            refuse('CURRENT_OWNER_EPOCH_INVALID')
        continuation.packets = deepcopy(facts.packets)


def source_ids(session):
    continuation = _ACTIVE.get()
    if continuation is None:
        return None
    continuation.check()
    if session is not continuation.facts.session:
        continuation.valid = False
        refuse('CURRENT_OWNER_EPOCH_CHANGED')
    return {row['source_id'] for row in continuation.facts.packets}


def invalidate(facts):
    continuation = getattr(facts, '_review_observation', None)
    if continuation is not None:
        continuation.valid = False
