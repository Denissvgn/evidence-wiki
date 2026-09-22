"""Plan application, observed readiness and conservative recovery boundaries."""



import os
from pathlib import Path

import pytest

from evidence_wiki._pack_io import canonical
from evidence_wiki.planning import compile_plan
from evidence_wiki.setup_application import apply_plan
from tests.test_research_planning import request

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Setup requires native POSIX descriptor locking.")

def test_empty_setup_and_replay_preserve_question(tmp_path):
    plan = compile_plan(canonical(request(tmp_path)))
    result = apply_plan(canonical(plan))
    assert result['status'] == 'ready', result
    assert result['setup_ready'] and result['evidence_empty']
    assert not result['research_complete'] and not result['claims_verified']
    assert result['strict']['effective_assurance'] == 'artifact_checked'
    assert result['strict']['reviewer_authenticated'] is False
    root = tmp_path / 'workspace'
    first = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    replay = apply_plan(canonical(plan))
    assert replay['status'] == 'ready', replay
    assert replay['transaction_id'] == result['transaction_id']
    assert first == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def local_request(root, *, suffix='.html', missing=False):
    value = request(root)
    inputs = root / 'originals'
    inputs.mkdir()
    source = inputs / ('paper' + suffix)
    if not missing:
        source.write_text('<html><head><title>Observed evidence</title></head><body><h1>Observed evidence</h1><p>Primary measured information with scope and units.</p></body></html>')
    payload = value['request']['payload']
    payload['sources'] = [{'id': 'local', 'kind': 'local_file', 'locator': str(source), 'question_ids': ['q1']}]
    payload['authority']['source_scope'] = [str(inputs)]
    payload['budgets']['bytes'] = 100000
    value['decisions']['source_requirements'] = [{'source_id': 'local', 'output_format': 'html' if suffix == '.html' else 'markdown',
                                               'needs_complete': True, 'scope': {'jurisdiction': 'Spain'}}]
    return value


@pytest.fixture
def in_process(monkeypatch):
    import contextlib
    import io

    from evidence_wiki import setup_application as app
    from evidence_wiki.pack_discovery import owner
    from evidence_wiki.setup_worker import execute

    def measured(operation, plan, clock, before, timeout):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute(operation, plan, clock)
        observation = owner('_evidence_authority').observed_execution(operation=operation, basis={'plan_id': plan['plan_id']},
            result=result, started_at=clock, finished_at=clock, exit_code=0)
        return result, observation
    monkeypatch.setattr(app, 'measured', measured)
    return measured


@pytest.mark.parametrize('suffix,expected', [('.html', 'ready'), ('.md', 'needs_input')])
def test_selected_delivery_has_observed_usability(tmp_path, in_process, suffix, expected):
    value = local_request(tmp_path, suffix=suffix)
    original = tmp_path / 'originals' / ('paper' + suffix)
    before = original.read_bytes()
    (original.parent / 'unselected.html').write_text('Do not copy this file')
    plan = compile_plan(canonical(value))
    result = apply_plan(canonical(plan))
    assert result['status'] == expected, result
    assert original.read_bytes() == before
    assert len(result['sources']) == 1
    assert result['sources'][0]['usable'] == (suffix == '.html')
    assert not list((tmp_path / 'workspace').rglob('unselected.html'))
    assert apply_plan(canonical(plan))['status'] == expected


def test_missing_input_preserved_as_question_gap(tmp_path, in_process):
    plan = compile_plan(canonical(local_request(tmp_path, missing=True)))
    result = apply_plan(canonical(plan))
    assert result['setup_ready'] and result['status'] == 'needs_input'
    assert result['blocked_routes'][0]['question_ids'] == ['q1']
    assert result['blocked_routes'][0]['reasons'] == ['local_source_absent']


@pytest.mark.parametrize('mutation', ['changed_plan', 'self_rehashed_plan', 'nonempty', 'symlink', 'host_protection', 'no_authority', 'interpreter', 'zero_time'])
def test_invalid_preconditions_do_not_change_target(tmp_path, monkeypatch, mutation):
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.planning import plan_identity

    value = request(tmp_path)
    if mutation == 'host_protection':
        value['request']['payload']['strict_evidence']['assurance'] = 'host_enforced'
    if mutation == 'no_authority':
        value['request']['payload']['authority']['allowed_actions'] = []
    if mutation == 'zero_time':
        value['request']['payload']['budgets']['seconds'] = 0
        with pytest.raises(EvidenceWikiError):
            compile_plan(canonical(value))
        assert not (tmp_path / 'workspace').exists()
        return
    plan = compile_plan(canonical(value))
    target = tmp_path / 'workspace'
    if mutation in {'changed_plan', 'self_rehashed_plan'}:
        plan['profile']['workspace_init']['project']['name'] = 'altered'
        if mutation == 'self_rehashed_plan':
            plan['plan_id'] = plan_identity(plan)
    elif mutation == 'nonempty':
        target.mkdir()
        (target / 'user.txt').write_text('Preserve')
    elif mutation == 'symlink':
        outside = tmp_path / 'elsewhere'
        outside.mkdir()
        target.symlink_to(outside, target_is_directory=True)
    elif mutation == 'interpreter':
        monkeypatch.setenv('EVIDENCE_WIKI_PYTHON', '/different/environment/bin/python')
    with pytest.raises(EvidenceWikiError):
        apply_plan(canonical(plan))
    assert not (target / 'research.yml').exists()
    if mutation == 'nonempty':
        assert (target / 'user.txt').read_text() == 'Preserve'


@pytest.mark.parametrize('step', ['initialize', 'intake', 'coverage', 'sources', 'inventory', 'normalize'])
@pytest.mark.parametrize('moment', ['before', 'after'])
def test_interruption_at_mutation_boundary(tmp_path, monkeypatch, in_process, step, moment):
    from evidence_wiki import setup_application as app
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.setup_store import snapshot

    plan = compile_plan(canonical(local_request(tmp_path)))
    class Crash(BaseException):
        pass
    def interrupted(operation, *args):
        if operation == step and moment == 'before':
            raise Crash()
        result = in_process(operation, *args)
        if operation == step and moment == 'after':
            raise Crash()
        return result
    monkeypatch.setattr(app, 'measured', interrupted)
    with pytest.raises(Crash):
        apply_plan(canonical(plan))
    before = snapshot(tmp_path / 'workspace')
    monkeypatch.setattr(app, 'measured', in_process)
    if moment == 'before':
        assert apply_plan(canonical(plan))['status'] == 'ready'
    else:
        with pytest.raises(EvidenceWikiError) as error:
            apply_plan(canonical(plan))
        assert error.value.error_code == 'ONBOARDING_OWNERSHIP_CONFLICT'
        assert snapshot(tmp_path / 'workspace') == before


@pytest.mark.parametrize('mutation', ['question_edit', 'extra', 'directory_replacement', 'corrupt_checkpoint', 'plan_drift', 'source_drift'])
def test_completed_setup_refuses_drift(tmp_path, in_process, mutation):
    import json
    import shutil

    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.setup_store import snapshot

    plan = compile_plan(canonical(local_request(tmp_path)))
    result = apply_plan(canonical(plan))
    target = tmp_path / 'workspace'
    if mutation == 'question_edit':
        (target / 'wiki/questions/q1.md').write_text('user changed this')
    elif mutation == 'extra':
        (target / 'user.txt').write_text('extra')
    elif mutation == 'directory_replacement':
        (target / 'wiki').rename(target / 'old-wiki')
        shutil.copytree(target / 'old-wiki', target / 'wiki')
        shutil.rmtree(target / 'old-wiki')
    elif mutation == 'corrupt_checkpoint':
        Path(result['checkpoint']).write_text('{')
    elif mutation == 'plan_drift':
        path = Path(result['checkpoint']).with_name('plan.json')
        saved = json.loads(path.read_text())
        saved['plan_id'] = 'a' * 64
        path.write_text(json.dumps(saved))
    else:
        (tmp_path / 'originals/paper.html').write_text('changed input')
    before = snapshot(target)
    with pytest.raises(EvidenceWikiError):
        apply_plan(canonical(plan))
    assert snapshot(target) == before


def test_busy_lock_and_dead_process_lock_file(tmp_path, in_process):
    from evidence_wiki.errors import EvidenceWikiError
    from evidence_wiki.setup_store import SetupStore

    plan = compile_plan(canonical(request(tmp_path)))
    with SetupStore(plan).locked():
        with pytest.raises(EvidenceWikiError) as error:
            apply_plan(canonical(plan))
        assert error.value.error_code == 'ONBOARDING_LOCK_BUSY'
    assert apply_plan(canonical(plan))['status'] == 'ready'


def test_corrupt_receipt_recomputed_and_failed_check_not_self_awarded(tmp_path, monkeypatch, in_process):
    import json

    from evidence_wiki import setup_application as app

    plan = compile_plan(canonical(request(tmp_path)))
    result = apply_plan(canonical(plan))
    receipt = Path(result['checkpoint']).with_name('receipt.json')
    receipt.write_text('caller supplied success')
    def fail_doctor(operation, *args):
        value, observation = in_process(operation, *args)
        if operation == 'doctor':
            value = {'status': 'failed', 'reason': 'observed_required_dependency_unavailable'}
        return value, observation
    monkeypatch.setattr(app, 'measured', fail_doctor)
    assert apply_plan(canonical(plan))['status'] == 'failed'
    assert json.loads(receipt.read_text())['setup_ready'] is False
    monkeypatch.setattr(app, 'measured', in_process)
    assert apply_plan(canonical(plan))['status'] == 'ready'


def test_readonly_computation_gap_does_not_dispatch(tmp_path, in_process):
    from tests._computation_fixture import aggregation, definition

    value = request(tmp_path)
    declaration = definition()
    declaration['aggregations']['observations'] = aggregation()
    value['decisions']['computation'] = declaration
    plan = compile_plan(canonical(value))
    result = apply_plan(canonical(plan))
    assert result['status'] == 'needs_input'
    assert result['computation']['status'] != 'passed'
    assert result['computation']['executed']
    assert result['computation']['clock']['as_of'] == '2026-09-21T12:00:00+00:00'
    assert len(list((tmp_path/'workspace/wiki/questions').glob('q*.md'))) == 1
    assert not (tmp_path/'workspace/sources/computation-state.json').exists()


def test_plugins_are_not_loaded_by_setup_checks(tmp_path, monkeypatch, in_process):
    from evidence_wiki.pack_discovery import owner

    monkeypatch.setattr(owner('doctor'), 'registration_report', lambda: pytest.fail('Unselected registration execution'))
    monkeypatch.setattr(owner('smoke_validate_workspace'), 'safe_registered_ids', lambda *_: pytest.fail('Unselected registration execution'))
    assert apply_plan(canonical(compile_plan(canonical(request(tmp_path)))))['status'] == 'ready'


def test_saved_receipt_publication_failure_can_resume(tmp_path, monkeypatch, in_process):
    from evidence_wiki.setup_store import SetupStore

    plan = compile_plan(canonical(request(tmp_path)))
    original = SetupStore.write
    def write(self, name, *args, **kwargs):
        if name == 'receipt.json':
            raise OSError('simulated interrupted publication')
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(SetupStore, 'write', write)
    with pytest.raises(OSError):
        apply_plan(canonical(plan))
    monkeypatch.setattr(SetupStore, 'write', original)
    assert apply_plan(canonical(plan))['status'] == 'ready'


def test_wrong_source_format_remains_gap(tmp_path, in_process):
    value = local_request(tmp_path)
    value['decisions']['source_requirements'][0]['output_format'] = 'pdf'
    result = apply_plan(canonical(compile_plan(canonical(value))))
    assert result['status'] == 'needs_input'
    assert result['sources'][0]['format_satisfied'] is False


def test_initial_empty_directory_is_supported(tmp_path, in_process):
    (tmp_path / 'workspace').mkdir()
    assert apply_plan(canonical(compile_plan(canonical(request(tmp_path)))))['status'] == 'ready'


def test_concurrent_unrelated_file_is_preserved_and_not_adopted(tmp_path, monkeypatch, in_process):
    from evidence_wiki import setup_application as app
    from evidence_wiki.errors import EvidenceWikiError

    plan = compile_plan(canonical(request(tmp_path)))
    def concurrent(operation, *args):
        value = in_process(operation, *args)
        if operation == 'intake':
            (tmp_path / 'workspace/user-note.txt').write_text('User-created during intake')
        return value
    monkeypatch.setattr(app, 'measured', concurrent)
    with pytest.raises(EvidenceWikiError) as caught:
        apply_plan(canonical(plan))
    assert caught.value.error_code == 'ONBOARDING_OWNERSHIP_CONFLICT'
    assert (tmp_path / 'workspace/user-note.txt').read_text() == 'User-created during intake'


def test_due_schedule_and_warning_are_readonly(tmp_path, in_process):
    from tests._computation_fixture import definition

    value = request(tmp_path)
    declaration = definition()
    declaration['invariants']['review'] = {'description': 'Review explicit warning', 'target': 'computed', 'selector': None,
        'inputs': {}, 'filter': None, 'assertion': '1 == 2', 'severity': 'warning', 'failure_message': 'Review the retained inputs'}
    declaration['cadence']['follow_up'] = {'description': 'Local status', 'trigger': {'type': 'fixed_date', 'at': '2026-09-20'},
        'lead_alerts': [], 'action': {'kind': 'status_flag', 'target': 'ready'}}
    value['decisions']['computation'] = declaration
    plan = compile_plan(canonical(value))
    result = apply_plan(canonical(plan))
    assert result['computation']['clock']['schedules'][0]['state'] == 'overdue'
    assert result['computation']['findings']
    assert len(list((tmp_path/'workspace/wiki/questions').glob('q*.md'))) == 1
    assert not list((tmp_path/'workspace').rglob('*computation-state*'))


def test_selected_bundled_pack_keeps_installed_identity(tmp_path, in_process):
    from evidence_wiki._pack_io import capture_pack
    from evidence_wiki._script_host import shared_assets_root
    from evidence_wiki.pack_discovery import snapshot_metadata

    value = request(tmp_path)
    info = snapshot_metadata(capture_pack(shared_assets_root() / 'domain-packs/general-science'))
    value['request']['payload']['domain'] = {'mode': 'domain_pack', 'rationale': 'Compare observed study evidence', 'pack': {
        'name': info['name'], 'version': info['version'], 'origin': 'bundled', 'locator': info['name'],
        **info['identity'], 'research_contract_version': info['compatible_research_yml_contract']}}
    value['request']['payload']['scope'] += [{'name': row['id'], 'value': 'Selected study scope'} for row in info['selection']['required_scope_inputs']]
    plan = compile_plan(canonical(value))
    result = apply_plan(canonical(plan))
    assert result['status'] == 'ready'
    assert result['pack']['tree_sha256'] == info['identity']['tree_sha256']
    assert (tmp_path/'workspace/domain-packs/.evidence-wiki-state.yml').is_file()
    assert apply_plan(canonical(plan))['status'] == 'ready'


def test_nested_existing_workspace_and_unsafe_journal_refuse(tmp_path, in_process):
    from evidence_wiki.errors import EvidenceWikiError

    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing/'research.yml').write_text('{}')
    (existing/'workspace-system.yml').write_text('{}')
    with pytest.raises(EvidenceWikiError):
        compile_plan(canonical(request(existing)))
    plan = compile_plan(canonical(request(tmp_path)))
    (tmp_path/'.evidence-wiki').symlink_to(existing, target_is_directory=True)
    with pytest.raises((OSError, EvidenceWikiError)):
        apply_plan(canonical(plan))
    assert not (tmp_path/'workspace').exists()
    assert sorted(path.name for path in existing.iterdir()) == ['research.yml', 'workspace-system.yml']


def test_cli_schemas_and_saved_plan(tmp_path, capsys):
    import json

    from evidence_wiki.cli import main
    from evidence_wiki.planning_commands import save_plan

    plan = compile_plan(canonical(request(tmp_path)))
    path = tmp_path/'plan.json'
    save_plan(plan, path)
    assert main(['agent','apply','--from-file',str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'ready'
    assert main(['agent','setup-schemas']) == 0
    assert 'evidence-setup-result/v1' in json.loads(capsys.readouterr().out)['schema_ids']


def test_delivered_source_has_blocked_outcome_if_inventory_fails(tmp_path, monkeypatch, in_process):
    from evidence_wiki import setup_application as app

    plan = compile_plan(canonical(local_request(tmp_path)))
    def fail_inventory(operation, *args):
        if operation == 'inventory':
            return {'status': 'failed'}, {'observation_id': 'sha256:' + 'a'*64}
        return in_process(operation, *args)
    monkeypatch.setattr(app, 'measured', fail_inventory)
    result = apply_plan(canonical(plan))
    assert result['status'] == 'failed'
    assert result['sources'][0]['input_id'] == 'local'
    assert result['sources'][0]['observations'][0]['delivery'] == 'captured'
    assert not result['sources'][0]['usable']


def test_historical_computation_clock_does_not_backdate_intake_or_authority(tmp_path, monkeypatch, in_process):
    from datetime import datetime, timezone

    from evidence_wiki.pack_discovery import owner
    from tests._computation_fixture import definition

    value = request(tmp_path)
    declaration = definition()
    declaration['clock']['as_of'] = '2001-01-01T12:00:00Z'
    value['decisions']['computation'] = declaration
    value['decisions']['trust'] = {'policy_id': 'selected-authority', 'policy_revision': '1'}
    observed = []
    monkeypatch.setattr(owner('_evidence_authority'), 'load_trust', lambda root, config, at: observed.append(at) or {})
    plan = compile_plan(canonical(value))
    result = apply_plan(canonical(plan))
    question = owner('question_status').load_frontmatter(tmp_path/'workspace/wiki/questions/q1.md')
    assert question['created'] == datetime.now(timezone.utc).date().isoformat()
    assert observed and observed[-1].year == datetime.now(timezone.utc).year
    assert result['computation']['clock']['as_of'] == '2001-01-01T12:00:00+00:00'
