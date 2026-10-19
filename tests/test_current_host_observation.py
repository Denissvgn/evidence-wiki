"""Full current snapshots and native host reads share effect-free owner custody."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki import execution_session as native
from evidence_wiki import host_qualification
from evidence_wiki.errors import ConfigError, UsageError
from evidence_wiki.mutation_contracts import digest
from evidence_wiki.research_session import ResearchSession
from tests.test_host_qualification import synthetic as _synthetic_fixture


@pytest.fixture
def observation(monkeypatch, tmp_path, request):
    session, records = _synthetic_fixture.__wrapped__(monkeypatch, tmp_path, request)
    state = {"reads": 0, "finishes": 0, "checks": [], "controls": {"context_id": "owned"},
        "changed": False, "denied": False, "qualified": True, "strict_reads": 0,
        "strict": {"basis_id": digest("strict-basis"), "claims": [{"accepted": False}], "evaluated_at": "opening"}}
    view = {"basis_id": digest("owned-basis"), "policy_id": session.policy.policy_id, "policy_compatible": True,
        "accounting": {"original_outcomes": [{"original_id": "original", "accepted": False}]},
        "canonical_release": {"publication": {"verdict": "hold"}}, "usage": {"seconds": 1},
        "qualification": {"state": "qualified", "profiles": []}}

    def check(_session, operation):
        state["checks"].append(operation)
        if operation != "current" or state["denied"]:
            raise UsageError("AUTHORITY_CHANGED", "Current native authority unavailable.")

    def finish():
        state["finishes"] += 1
        if state["changed"]:
            raise UsageError("SOURCE_CHANGED", "Current native source changed.")

    def observe(_session):
        state["reads"] += 1
        observed = SimpleNamespace(config={}, controls={"result": {"context_id": "owned"}}, finish=finish,
            policy={"id": "owned"}, integrations_qualified=True,
            strict={"state": "observed", "result": copy.deepcopy(state["strict"])})
        return copy.deepcopy(view), observed, [], copy.deepcopy(records)

    def strict_check(_root, operation):
        assert operation == "check"
        state["strict_reads"] += 1
        return copy.deepcopy(state["strict"]) | {"evaluated_at": "closing"}

    monkeypatch.setattr(ResearchSession, "_check", check)
    monkeypatch.setattr(native, "_observe_current", observe)
    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(capture=lambda *_: state["controls"])
        if name == "_caller_context" else SimpleNamespace(configured=lambda *_: False)
        if name == "_evidence_usage" else SimpleNamespace(run_operation=strict_check)
        if name == "strict_evidence" else SimpleNamespace(configuration=lambda *_: {}))
    monkeypatch.setattr(native, "_qualifications", lambda *_a, **_k: {
        "state": "qualified" if state["qualified"] else "unknown", "profiles": []})
    from evidence_wiki import service_authentication
    original_status = service_authentication.status

    def status(selected):
        before = native._current_identity(selected)
        result = original_status(selected)
        after = native._current_identity(selected)
        assert before == after
        return result

    monkeypatch.setattr(service_authentication, "status", status)
    return session, state, records, view


def test_full_current_and_native_witness_share_one_observation_with_repeated_validity_checks(observation):
    session, state, _, expected = observation
    with native._current_facts(session, operation="current") as facts:
        view = native._current_view(session)
        assert view == expected and view is not facts.view
        view["canonical_release"]["publication"]["verdict"] = "caller-mutated"
        assert facts.view["canonical_release"]["publication"]["verdict"] == "hold"
        witness = host_qualification(session)
        assert witness["basis_id"] == expected["basis_id"]
        assert witness["attempts"] == [] and witness["authorized"] is False
        assert native.current_qualifications(session)["state"] == "qualified"
    assert state["reads"] == 1 and state["strict_reads"] == 1
    assert state["finishes"] >= 12 and set(state["checks"]) == {"current"}
    assert facts.invalidated is True


def test_current_grant_is_required_before_observation_without_resolution_permission(observation):
    session, state, _, _ = observation
    state["denied"] = True
    with pytest.raises(UsageError, match="authority unavailable"):
        with native._current_facts(session, operation="current"):
            pytest.fail("Ungrantable observation opened")
    assert state["reads"] == 0 and state["checks"] == ["current"]


@pytest.mark.parametrize("operation", ["resolution", "semantic_report"])
def test_full_view_requires_the_epoch_with_full_semantic_closing(observation, monkeypatch, operation):
    session, _, _, expected = observation
    monkeypatch.setattr(ResearchSession, "_check", lambda *_args, **_kwargs: None)
    with native._current_facts(session, operation=operation):
        with pytest.raises(UsageError):
            native._current_view(session)
        assert native._current_identity(session)["basis_id"] == expected["basis_id"]


@pytest.mark.parametrize("change", ["source", "journal", "context", "authority", "effect"])
def test_current_host_epoch_rechecks_native_changes_before_next_read(observation, change):
    session, state, records, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            native._current_view(session)
            if change == "source":
                state["changed"] = True
            elif change == "journal":
                records.append({"kind": "changed"})
            elif change == "context":
                state["controls"] = {"context_id": "changed"}
            elif change == "authority":
                state["denied"] = True
            else:
                native.invalidate_current_facts()
            host_qualification(session)
    assert state["reads"] == 1


def test_full_snapshot_refreshes_qualification_instead_of_reusing_opening_state(observation):
    session, state, _, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            assert native._current_view(session)["qualification"]["state"] == "qualified"
            state["qualified"] = False
            assert native._current_view(session)["qualification"]["state"] == "unknown"
            assert facts.view["qualification"]["state"] == "qualified"
    assert state["reads"] == 1


def test_witness_refuses_qualification_change_during_native_credential_read(observation, monkeypatch):
    from evidence_wiki import service_authentication

    session, state, _, _ = observation
    status = service_authentication.status
    def expiring(selected):
        result = status(selected)
        state["qualified"] = False
        return result
    monkeypatch.setattr(service_authentication, "status", expiring)
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            with pytest.raises(ConfigError) as caught:
                host_qualification(session)
            assert caught.value.details["reason"] == "observation_changed"


@pytest.mark.parametrize("change", ["source", "journal", "context", "authority", "effect"])
def test_epoch_closing_refuses_changes_after_a_returned_witness(observation, change):
    session, state, records, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            witness = host_qualification(session)
            assert witness["authorized"] is False
            if change == "source":
                state["changed"] = True
            elif change == "journal":
                records.append({"kind": "changed"})
            elif change == "context":
                state["controls"] = {"context_id": "changed"}
            elif change == "authority":
                state["denied"] = True
            else:
                native.invalidate_current_facts()
    assert facts.invalidated is True and native._CURRENT_FACTS.get() is None


def test_full_snapshot_cannot_cross_sessions_or_reenter_an_exited_epoch(observation):
    session, state, _, _ = observation
    with native._current_facts(session, operation="current") as facts:
        with pytest.raises(UsageError):
            native._current_view(copy.copy(session))
    token = native._CURRENT_FACTS.set(facts)
    try:
        with pytest.raises(UsageError):
            native._current_view(session)
    finally:
        native._CURRENT_FACTS.reset(token)
    with native._current_facts(session, operation="current"):
        native._current_view(session)
    assert state["reads"] == 2


@pytest.mark.parametrize("change", ["strict_source_expired", "strict_source_outside_effective_interval",
    "strict_source_age_invalid", "strict_review_expired", "qualification", "material"])
def test_native_strict_closing_revalidates_semantic_time_and_owner_boundaries(observation, monkeypatch, change):
    session, state, _, _ = observation
    actual_owner = native.owner

    def strict_check(_root, operation):
        state["strict_reads"] += 1
        assert operation == "check"
        if change == "qualification":
            state["qualified"] = False
        elif change == "material":
            state["changed"] = True
        else:
            raise UsageError("STRICT_EVIDENCE_REFUSED", "Native strict closing refused.", details={"reason": change})
        return copy.deepcopy(state["strict"]) | {"evaluated_at": "closing"}

    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(run_operation=strict_check)
        if name == "strict_evidence" else actual_owner(name))
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current") as facts:
            view = native._current_view(session)
            witness = host_qualification(session)
            assert view["qualification"]["state"] == "qualified" and witness["authorized"] is False
    assert state["strict_reads"] == 1 and facts.invalidated is True


def test_closing_strict_compares_all_semantic_fields_and_only_ignores_evaluation_time(observation):
    session, state, _, _ = observation
    with pytest.raises(UsageError):
        with native._current_facts(session, operation="current"):
            state["strict"]["claims"][0]["accepted"] = True
    assert state["strict_reads"] == 1


@pytest.mark.parametrize("availability", ["not_configured", "unavailable"])
def test_current_epoch_preserves_same_legitimate_strict_availability(observation, monkeypatch, availability):
    session, state, _, _ = observation
    observe = native._observe_current
    strict_state = {"state": availability}
    if availability == "unavailable":
        strict_state.update(reason="strict_policy_missing", error_code="STRICT_EVIDENCE_REFUSED")

    def unavailable(selected):
        view, observed, packets, records = observe(selected)
        observed.strict = strict_state
        if availability == "not_configured":
            observed.policy = None
        return view, observed, packets, records

    actual_owner = native.owner
    def refused(*_args):
        state["strict_reads"] += 1
        raise UsageError("STRICT_EVIDENCE_REFUSED", "Native strict unavailable.", details={"reason": "strict_policy_missing"})

    monkeypatch.setattr(native, "_observe_current", unavailable)
    monkeypatch.setattr(native, "owner", lambda name: SimpleNamespace(run_operation=refused)
        if name == "strict_evidence" else actual_owner(name))
    with native._current_facts(session, operation="current"):
        native._current_view(session)
    assert state["strict_reads"] == (1 if availability == "unavailable" else 0)


@pytest.fixture
def owned_sources(tmp_path, monkeypatch):
    """Use genuine native custody and retrieval with bounded local test services."""
    from contextlib import ExitStack
    from dataclasses import replace

    from evidence_wiki import Workspace, execution_evidence
    from evidence_wiki import _review_dependency_inputs as inputs
    from evidence_wiki.pack_discovery import owner
    from evidence_wiki.research_observation import ResearchObservation
    from evidence_wiki.source_inputs import SourceView
    from tests.test_review_packet_execution import configured

    root, policy, execution, context, claim, _ = configured.__wrapped__(tmp_path, monkeypatch)
    context = replace(context, grants=context.grants | {'host_qualification.current', 'service_authentication.status',
        'research_session.resolution', 'research_session.semantic_report'})
    state = {'constructors': 0, 'sources': [], 'finishes': 0, 'source_finishes': 0, 'usage': 0, 'source_scopes': []}
    original_init, original_finish = ResearchObservation.__init__, ResearchObservation.finish
    original_sources, original_source_finish = execution_evidence.sources, SourceView.finish
    usage = owner('_evidence_usage')
    original_usage = usage.UsageView.revalidate

    def initialize(self, *args, **kwargs):
        state['constructors'] += 1
        original_init(self, *args, **kwargs)

    def finish(self):
        state['finishes'] += 1
        return original_finish(self)

    def sources(session, observed):
        state['sources'].append(observed)
        state['source_scopes'].append(owner('_publication_context').live_read.get())
        return original_sources(session, observed)

    def source_finish(self):
        state['source_finishes'] += 1
        return original_source_finish(self)

    def revalidate(self, *args):
        state['usage'] += 1
        return original_usage(self, *args)

    monkeypatch.setattr(ResearchObservation, '__init__', initialize)
    monkeypatch.setattr(ResearchObservation, 'finish', finish)
    monkeypatch.setattr(execution_evidence, 'sources', sources)
    monkeypatch.setattr(SourceView, 'finish', source_finish)
    monkeypatch.setattr(usage.UsageView, 'revalidate', revalidate)
    with ExitStack() as stack:
        stack.enter_context(context.activate())
        workspace = stack.enter_context(Workspace.open(root))
        session = stack.enter_context(workspace.research.session(policy, run_id='owned-source-context', execution=execution))
        yield SimpleNamespace(session=session, state=state, bind=inputs.bind_session, claim=claim,
            original_sources=original_sources, execution_evidence=execution_evidence)


def test_owned_source_context_freshly_retrieves_identical_packets_and_closes_native_guards(owned_sources):
    case = owned_sources
    expected = native._host_sources(case.session)
    before = {key: value for key, value in case.state.items() if key != 'sources'} | {'source_count': len(case.state['sources'])}
    with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
        opening = dict(case.state)
        actual = native._host_sources(case.session)
        assert actual == expected and actual[0] is not facts.packets
        assert case.state['constructors'] == before['constructors'] + 1
        assert case.state['sources'][-1] is facts.observed
        assert case.state['source_scopes'][-1] is None
        assert len(case.state['sources']) == before['source_count'] + 2
        assert case.state['finishes'] >= opening['finishes'] + 2
        assert case.state['source_finishes'] > opening['source_finishes']
        assert case.state['usage'] > opening['usage']
    assert facts.invalidated


@pytest.mark.parametrize('change', ['source', 'config', 'requirements', 'root', 'journal', 'effect', 'callback',
    'capture', 'observed', 'requirements_object', 'context', 'thread', 'task', 'execution', 'usage'])
@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_owned_source_context_refuses_live_and_captured_changes(owned_sources, monkeypatch, change, boundary):
    from evidence_wiki import _review_dependency_inputs as inputs
    from evidence_wiki import research_execution
    from evidence_wiki.pack_discovery import owner

    case = owned_sources
    session = case.session
    restored = []

    def alter(facts):
        if change in {'source', 'config', 'requirements'}:
            relative = ('research.yml' if change == 'config' else 'docs/research-requirements.json'
                if change == 'requirements' else next(name for name in facts.observed.capture.files
                    if name.startswith('sources/normalized/') and name.endswith('.md')))
            path = session.root / relative
            raw = path.read_bytes()
            restored.append(lambda: path.write_bytes(raw))
            path.write_bytes(raw + b'\n ')
        elif change == 'root':
            original = session._root
            session._root = session.root.parent
            restored.append(lambda: setattr(session, '_root', original))
        elif change == 'journal':
            original = session.execution.records
            monkeypatch.setattr(session.execution, 'records', lambda _session: [*original(_session), {'changed': True}])
        elif change == 'effect':
            native.invalidate_current_facts()
        elif change == 'callback':
            assert research_execution.bounded_host_call(lambda: None, (), 1) == (None, None)
        elif change == 'capture':
            facts.observed.capture = SimpleNamespace(files=dict(facts.observed.capture.files))
        elif change == 'observed':
            facts.observed = copy.copy(facts.observed)
        elif change == 'requirements_object':
            facts.observed.requirements['decisions']['changed'] = True
        elif change == 'context':
            original = session._context
            session._context = copy.copy(original)
            restored.append(lambda: setattr(session, '_context', original))
        elif change == 'thread':
            monkeypatch.setattr(inputs.threading, 'get_ident', lambda: -1)
        elif change == 'task':
            monkeypatch.setattr(inputs, '_task', object)
        elif change == 'execution':
            original = session._execution
            session._execution = copy.copy(original)
            restored.append(lambda: setattr(session, '_execution', original))
        else:
            usage = owner('_evidence_usage')
            def withdrawn(*args):
                raise UsageError('USAGE_CHANGED', 'Controlled usage permission withdrawn.')
            monkeypatch.setattr(usage.UsageView, 'revalidate', withdrawn)

    changes = monkeypatch.context()
    monkeypatch = changes.__enter__()
    try:
        with pytest.raises((UsageError, ConfigError, ValueError)):
            with case.bind(session), native._current_facts(session, operation='current') as facts:
                if boundary == 'before':
                    alter(facts)
                else:
                    original = case.execution_evidence.sources
                    def changed(*args):
                        result = original(*args)
                        alter(facts)
                        return result
                    monkeypatch.setattr(case.execution_evidence, 'sources', changed)
                native._host_sources(session)
        assert facts.invalidated
    finally:
        changes.__exit__(None, None, None)
        for restore in reversed(restored):
            restore()


@pytest.mark.parametrize('foreign', ['session', 'context', 'exited', 'fabricated', 'copied_facts'])
def test_owned_source_context_refuses_foreign_or_expired_custody(owned_sources, foreign):
    from contextvars import copy_context

    case = owned_sources
    session = case.session
    with pytest.raises((UsageError, ValueError)):
        with case.bind(session), native._current_facts(session, operation='current') as facts:
            if foreign == 'session':
                native._host_sources(copy.copy(session))
            elif foreign == 'context':
                copy_context().run(native._host_sources, session)
            elif foreign == 'exited':
                captured = copy_context()
            else:
                token = native._CURRENT_FACTS.set(SimpleNamespace() if foreign == 'fabricated' else copy.copy(facts))
                try:
                    native._host_sources(session)
                finally:
                    native._CURRENT_FACTS.reset(token)
        if foreign == 'exited':
            captured.run(native._host_sources, session)


@pytest.mark.parametrize('operation', ['resolution', 'semantic_report'])
def test_non_current_source_read_retains_standalone_constructor(owned_sources, operation):
    case = owned_sources
    with case.bind(case.session), native._current_facts(case.session, operation=operation) as facts:
        before = case.state['constructors']
        result = native._host_sources(case.session)
        assert result[0] == facts.packets
        assert case.state['constructors'] == before + 1
        assert case.state['sources'][-1] is not facts.observed


@pytest.mark.parametrize('failure', [KeyboardInterrupt, SystemExit, TimeoutError])
def test_owned_source_failure_preserves_exception_and_invalidates_custody(owned_sources, monkeypatch, failure):
    case = owned_sources
    marker = failure('controlled source boundary')
    with pytest.raises(failure) as caught:
        with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
            def unavailable(*args):
                raise marker
            monkeypatch.setattr(case.execution_evidence, 'sources', unavailable)
            native._host_sources(case.session)
    assert caught.value is marker and facts.invalidated


def test_source_result_cannot_escape_failed_final_semantic_close(owned_sources, monkeypatch):
    case = owned_sources
    escaped = []
    actual_owner = native.owner
    strict = actual_owner('strict_evidence')
    actual = strict.run_operation
    def changed(*args, **kwargs):
        result = actual(*args, **kwargs)
        return result | {'basis_id': digest('changed-strict-basis')}
    def composite():
        with case.bind(case.session), native._current_facts(case.session, operation='current'):
            result = native._host_sources(case.session)
            monkeypatch.setattr(strict, 'run_operation', changed)
        escaped.append(result)
    with pytest.raises(UsageError):
        composite()
    assert escaped == []


def test_owned_source_context_preserves_valid_default_and_explicit_interpreter_selection(owned_sources, monkeypatch):
    import sys

    case = owned_sources
    with case.bind(case.session), native._current_facts(case.session, operation='current'):
        before = case.state['constructors']
        monkeypatch.delenv('EVIDENCE_WIKI_PYTHON', raising=False)
        default = native._host_sources(case.session)
        monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', sys.executable)
        assert native._host_sources(case.session) == default
        assert case.state['constructors'] == before


@pytest.mark.parametrize('boundary', ['before', 'after'])
def test_owned_source_context_preserves_fresh_interpreter_refusal(owned_sources, monkeypatch, boundary):
    case = owned_sources
    with pytest.raises(UsageError) as caught:
        with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
            if boundary == 'before':
                monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', str(case.session.root / 'foreign-interpreter'))
            else:
                original = case.execution_evidence.sources
                def changed(*args):
                    result = original(*args)
                    monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', str(case.session.root / 'foreign-interpreter'))
                    return result
                monkeypatch.setattr(case.execution_evidence, 'sources', changed)
            native._host_sources(case.session)
    assert caught.value.details['field'] == 'caller_selected_interpreter_mismatch'
    assert facts.invalidated


def test_owned_source_context_stays_invalid_after_caught_refusal_and_retry(owned_sources, monkeypatch):
    case = owned_sources
    with pytest.raises(UsageError):
        with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
            monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', str(case.session.root / 'foreign-interpreter'))
            with pytest.raises(UsageError):
                native._host_sources(case.session)
            assert facts.invalidated
            monkeypatch.delenv('EVIDENCE_WIKI_PYTHON')
            with pytest.raises(UsageError):
                native._host_sources(case.session)


def test_standalone_source_context_keeps_interpreter_constructor_admission(owned_sources, monkeypatch):
    case = owned_sources
    monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', str(case.session.root / 'foreign-interpreter'))
    with pytest.raises(UsageError) as caught:
        native._host_sources(case.session)
    assert caught.value.details['field'] == 'caller_selected_interpreter_mismatch'


def test_contextual_native_host_witness_uses_owned_context_with_identical_evidence(owned_sources):
    from evidence_wiki.review_packet_execution import reserve_evaluator
    from evidence_wiki.review_page_session import assembled

    case = owned_sources
    assembled(case.session, request_id='retained-pages', claim_id=case.claim['id'], page_chars=1024)
    attempt = reserve_evaluator(case.session, request_id='retained-evaluator', assembly_request_id='retained-pages',
        original_id='q', claim_id=case.claim['id'], config_ref='profile-a')
    expected = host_qualification(case.session)
    expected_current = case.session.current()
    assert any(row['attempt_id'] == attempt.attempt_id for row in expected['attempts'])
    before = case.state['constructors']
    with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
        current = native._current_view(case.session)
        source_reads = len(case.state['sources'])
        actual = host_qualification(case.session)
        qualification = native.current_qualifications(case.session)
        assert len(case.state['sources']) == source_reads + 1
        assert case.state['sources'][-1] is facts.observed
        assert case.state['constructors'] == before + 1
        for key in ('basis_id', 'policy_id', 'attempts', 'control', 'capability_matrix', 'authorized', 'evidence_accepted'):
            assert actual[key] == expected[key]
        for key in ('basis_id', 'policy_id', 'policy_compatible', 'qualification', 'execution', 'accounting'):
            assert current[key] == expected_current[key]
        assert qualification == expected_current['qualification']


@pytest.mark.parametrize('change', ['normalized', 'raw', 'symlink', 'authority'])
def test_fresh_source_body_closes_real_bytes_paths_and_authority(owned_sources, monkeypatch, change):
    import json

    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.pack_discovery import owner

    case = owned_sources
    restore = []
    entered = []
    original = case.execution_evidence._documents
    def changed(view, selected, **kwargs):
        result = original(view, selected, **kwargs)
        fact = next(iter(result[1].values()))
        path = case.session.root / fact['document'].path
        if change == 'raw':
            record = view.manifest()[fact['source_id']]
            path = owner('source_inventory').raw_fingerprint_paths(case.session.root, record)[0]
        elif change == 'authority':
            path = case.session.context.authority_file
        raw = path.read_bytes()
        def recover():
            if path.is_symlink():
                path.unlink()
            path.write_bytes(raw)
        restore.append(recover)
        if change == 'symlink':
            alternate = path.with_name(path.name + '.alternate')
            alternate.write_bytes(raw)
            path.unlink()
            path.symlink_to(alternate)
            restore.append(lambda: alternate.unlink())
        elif change == 'authority':
            document = json.loads(raw)
            document['expires_at'] = '2020-01-01T00:00:00Z'
            path.write_text(json.dumps(document))
        else:
            path.write_bytes(raw + b'\nChanged source bytes.\n')
        entered.append(change)
        return result
    try:
        with pytest.raises((EvidenceWikiError, ValueError)):
            with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
                monkeypatch.setattr(case.execution_evidence, '_documents', changed)
                native._host_sources(case.session)
        assert entered == [change] and facts.invalidated
    finally:
        for recover in reversed(restore):
            recover()


@pytest.mark.parametrize('malformed', [False, True])
def test_owned_source_context_preserves_default_validation_of_unrelated_runs(owned_sources, malformed):
    from evidence_wiki import Workspace

    case = owned_sources
    session = case.session
    with native.gate(session):
        native._begin(session)
    with Workspace.open(session.root) as workspace:
        with workspace.research.session(session.policy, run_id='unrelated-retained', execution=session.execution) as unrelated:
            with native.gate(unrelated):
                native._begin(unrelated)
    path = session.root / 'runs/unrelated-retained/run-state.json'
    raw = path.read_bytes()
    try:
        if malformed:
            path.write_bytes(b'{}\n')
            with pytest.raises(Exception) as original:
                native._sources(session)
            entered = []
            with pytest.raises(UsageError):
                with case.bind(session), native._current_facts(session, operation='current') as facts:
                    entered.append(True)
                    with pytest.raises(type(original.value)):
                        native._host_sources(session)
                    assert facts.invalidated
            assert entered == [True]
        else:
            expected = native._sources(session)
            with case.bind(session), native._current_facts(session, operation='current'):
                assert native._host_sources(session) == expected
    finally:
        path.write_bytes(raw)


def test_owned_source_context_preserves_native_constructor_scope_and_sticky_failure(owned_sources):
    from evidence_wiki.pack_discovery import owner

    case = owned_sources
    usage = owner('_evidence_usage')
    expected = type(owner('_usage_gate').refusal('usage_observation_scope_nested'))
    for reader in (native._sources, native._host_sources):
        completed = []
        with pytest.raises(ValueError, match='usage_observation_scope_nested'):
            with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
                before = len(case.state['sources'])
                with usage._observation_reads(case.session.root, facts.observed.config):
                    with pytest.raises(expected) as caught:
                        reader(case.session)
                    assert caught.value.details['reason'] == 'usage_observation_scope_nested'
                    assert len(case.state['sources']) == before
                    if reader is native._host_sources:
                        assert facts.invalidated
                        with pytest.raises(UsageError):
                            reader(case.session)
                    with pytest.raises(ValueError, match='usage_observation_scope_nested'):
                        with usage.current_view(case.session.root, facts.observed.config):
                            pytest.fail('Poisoned native scope reopened')
                    completed.append(True)
        assert completed == [True]


def test_owned_source_context_preserves_constructor_cross_view_clock_refusal(owned_sources, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from evidence_wiki.pack_discovery import owner

    case = owned_sources
    usage = owner('_evidence_usage')
    status = owner('workspace_status')
    original_status = status.run_status_report
    origin = datetime.now(timezone.utc)
    clock = {'seconds': 20}
    observed, completed = [], []
    expected = type(owner('_usage_gate').refusal('host_state_clock_regressed'))

    class Clock(datetime):
        @classmethod
        def now(cls, zone=None):
            return origin + timedelta(seconds=clock['seconds'])

    def regressing(*args, **kwargs):
        result = original_status(*args, **kwargs)
        observed.append(owner('_publication_context').live_read.get())
        clock['seconds'] = 10
        return result

    with pytest.raises(UsageError):
        with case.bind(case.session), native._current_facts(case.session, operation='current') as facts:
            with monkeypatch.context() as changes:
                changes.setattr(usage, 'datetime', Clock)
                changes.setattr(status, 'run_status_report', regressing)
                before = len(case.state['sources'])
                with pytest.raises(expected) as original:
                    native._sources(case.session)
                assert original.value.details['reason'] == 'host_state_clock_regressed'
                clock['seconds'] = 20
                with pytest.raises(expected) as owned:
                    native._host_sources(case.session)
                assert owned.value.details == original.value.details
                assert facts.invalidated and len(case.state['sources']) == before
            assert len(observed) == 2 and all(value is not None for value in observed)
            completed.append(True)
    assert completed == [True]
