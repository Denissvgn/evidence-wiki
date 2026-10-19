"""Successful source parsing reuse stays within its native evaluator scope."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_wiki import _review_dependency_inputs as native
from evidence_wiki.errors import EvidenceWikiError
from tests.test_dependency_counter_scope import evaluator_owner as evaluator_owner
from tests.test_dependency_counter_scope import native_evaluation
from tests.test_dependency_counter_scope import scope_inputs as scope_inputs


@pytest.fixture
def source_inputs(scope_inputs, monkeypatch):
    from evidence_wiki import pack_discovery, retrieval_operations
    holder, args, _, _, _ = scope_inputs
    root, config, capture, manifest, view = args
    capture.files['record.md'] = b'original'
    state = {'bytes': b'original', 'allowed': True, 'complete': True, 'reads': 0, 'qualified': 0,
             'fail': False, 'revision': 'revision', 'finished': 0}

    class SourceView:
        def __init__(self, root):
            self.files = {'record.md': state['bytes']}
        def manifest(self):
            return ['source']
        def recheck_scope(self):
            if not state['allowed']:
                raise PermissionError('source denied')
        def finish(self):
            state['finished'] += 1
            if self.files['record.md'] != state['bytes']:
                raise ValueError('source changed')

    def documents(source_view, selected, *, usage_view):
        state['reads'] += 1
        if state['fail']:
            raise ValueError('failed native read')
        return [], {'record.md': {'source_id': 'source', 'document': SimpleNamespace(path='record.md'),
                    'normalized_sha256': 'digest'}}, 0, False

    def qualify(*unused, **kwargs):
        state['qualified'] += 1
        assert kwargs == {'purpose': 'qa-export', 'consumer': 'evidence-wiki'}
        if not state['complete']:
            raise ValueError('incomplete current qualification')
        return {'selected': {'source': state['revision']}}

    original = pack_discovery.owner
    def owner(name):
        if name == '_publication_usage':
            return SimpleNamespace(qualify_capture=qualify)
        if name == '_normalized_contract':
            return SimpleNamespace(split_record=lambda raw: ({'nested': {'value': 'original'}}, raw, None))
        return original(name)

    monkeypatch.setattr(pack_discovery, 'owner', owner)
    monkeypatch.setattr(retrieval_operations, '_ScopedView', SourceView)
    monkeypatch.setattr(retrieval_operations, '_documents', documents)
    return holder, args, state


def consume(holder, args):
    root, config, capture, _, view = args
    return holder.source_documents(root, config, capture, view)


def test_success_reuses_only_parsing_and_detaches_every_return(source_inputs):
    holder, args, state = source_inputs
    with native_evaluation(holder, args):
        first = consume(holder, args)
        expected = deepcopy(first)
        first[0]['metadata']['nested']['value'] = 'changed'
        assert consume(holder, args) == expected
        assert state['reads'] == 1
        assert state['qualified'] == 2
    assert native._COUNTERS.get() is None
    consume(holder, args)
    consume(holder, args)
    assert state['reads'] == 3
    with native_evaluation(holder, args):
        consume(holder, args)
    assert state['reads'] == 4


@pytest.mark.parametrize('mutation', ['permission', 'incomplete', 'bytes', 'revision'])
def test_live_source_and_qualification_changes_never_return_cached_facts(source_inputs, mutation):
    holder, args, state = source_inputs
    with native_evaluation(holder, args):
        consume(holder, args)
        scope = native._COUNTERS.get()
        key, value = {'permission': ('allowed', False), 'incomplete': ('complete', False),
                      'bytes': ('bytes', b'changed'), 'revision': ('revision', 'changed')}[mutation]
        state[key] = value
        with pytest.raises((PermissionError, ValueError, EvidenceWikiError)):
            consume(holder, args)
        assert scope._source_documents is None
        assert scope._source_view is None
        assert state['reads'] == 1


@pytest.mark.parametrize('mutation', ['capture', 'manifest', 'state', 'trust', 'configuration'])
def test_changed_scope_clears_retained_native_sources(source_inputs, mutation):
    holder, args, state = source_inputs
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        consume(holder, args)
        scope = native._COUNTERS.get()
        if mutation == 'capture':
            args[2].files['record.md'] = b'changed'
        elif mutation == 'manifest':
            args[3]['claims'][0]['id'] = 'changed'
        elif mutation == 'state':
            args[4].state.checkpoint = 'changed'
        elif mutation == 'trust':
            args[4].trust['hash'] = 'changed'
        else:
            args[1]['configured'] = False
        with pytest.raises(EvidenceWikiError):
            consume(holder, args)
        assert scope._source_documents is None
        assert scope._source_view is None
        consume(holder, args)


def test_failed_first_read_is_not_retained_and_can_retry(source_inputs):
    holder, args, state = source_inputs
    with native_evaluation(holder, args):
        state['fail'] = True
        with pytest.raises(ValueError):
            consume(holder, args)
        assert native._COUNTERS.get()._source_documents is None
        state['fail'] = False
        assert consume(holder, args)[0]['content'] == 'original'
        assert state['reads'] == 2


def test_factory_rejects_nonowner_before_context_manager_entry(source_inputs):
    holder, args, _ = source_inputs
    with pytest.raises(EvidenceWikiError):
        holder.counter_scope(*args)


@pytest.mark.parametrize('escape', ['context', 'thread', 'task', 'lifetime'])
def test_source_facts_do_not_escape_native_scope(source_inputs, escape):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    holder, args, state = source_inputs
    if escape == 'lifetime':
        with native_evaluation(holder, args):
            consume(holder, args)
            copied = copy_context()
            scope = native._COUNTERS.get()
        with pytest.raises(EvidenceWikiError):
            copied.run(consume, holder, args)
        assert scope._source_view is None
        return
    with pytest.raises(EvidenceWikiError), native_evaluation(holder, args):
        consume(holder, args)
        scope = native._COUNTERS.get()
        copied = copy_context()
        if escape == 'context':
            with pytest.raises(ValueError):
                copied.run(consume, holder, args)
        elif escape == 'thread':
            with ThreadPoolExecutor(max_workers=1) as executor:
                with pytest.raises(EvidenceWikiError):
                    executor.submit(copied.run, consume, holder, args).result()
        else:
            async def child():
                consume(holder, args)
            with pytest.raises(EvidenceWikiError):
                asyncio.run(child())
        assert scope._source_documents is None
        assert state['reads'] == 1
        consume(holder, args)
