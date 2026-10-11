"""Immutable declarations preserve detached schemas and per-call validation."""

import copy
import hashlib
import json

import pytest

from evidence_wiki import workflow_contracts as owner
from tests.test_workflow_contracts import DEADLINE, NOW, job, result

SCHEMA_DIGESTS = {
    owner.JOB: '7c38fa8775b2f1528fb6ce2bd8d8b444c8972e085cc3254e34f66f2ac53b17c5',
    owner.JOB_V2: '1e9968de26959e4380966748887d67dddaff7a49e2ca5ebab992da83597b1c29',
    owner.JOB_V3: '9462041e2df4995c029c1eb9b504bc642b62793d49bec4b1080d2a1384a0dfe7',
    owner.JOB_V4: '728c91a9c5bb60ee43743b61f6dc314916015b3bed288636573288c61397a322',
    owner.RESULT: 'a70b62ca82a58ba270210eb05ee402b8174aaedadea70b85d646ff9fe75d1acf',
    owner.DECISION: '0664ed4a5dbbaf82853a9c6304733fcfc572c8f6499231eb7cc5fc201bece861',
}


@pytest.fixture
def cold_schema_cache():
    owner._schema_bytes.cache_clear()
    yield
    owner._schema_bytes.cache_clear()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()


@pytest.mark.parametrize('name', SCHEMA_DIGESTS)
def test_closed_schema_bytes_match_original_independent_vectors(name):
    assert hashlib.sha256(canonical(owner.schema_document(name))).hexdigest() == SCHEMA_DIGESTS[name]
    assert owner.schemas()[name] == owner.schema_document(name)


def test_returned_schema_and_catalog_mutation_cannot_change_private_declarations():
    original = owner.schemas()
    exposed = owner.schemas()
    for value in exposed.values():
        value.clear()
    exposed.clear()
    selected = owner.schema_document(owner.JOB_V3)
    selected['anyOf'][0]['required'].clear()
    selected['anyOf'][0]['properties']['qualifications']['properties'].clear()
    catalog = owner.schema_catalog()
    catalog['roles'].append('unselected-role')
    catalog['limits']['bytes'] = 0
    assert owner.schemas() == original
    assert owner.schema_catalog()['roles'] == list(owner.ROLES)
    assert owner.schema_catalog()['limits']['bytes'] == owner.MAX_BYTES
    assert all(type(raw) is bytes for _name, raw in owner._schema_bytes())


def test_only_declaration_construction_is_cached_while_each_document_is_checked(monkeypatch, cold_schema_cache):
    counts = {'build': 0, 'tree': 0, 'match': 0, 'semantic': 0}
    for name, key in [('_build_schemas', 'build'), ('_bounded_tree', 'tree'),
                      ('_matches', 'match'), ('_semantic', 'semantic')]:
        original = getattr(owner, name)

        def counted(*args, _original=original, _key=key, **kwargs):
            counts[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)
    owner.schema_catalog()
    assert counts['build'] == 1
    before = counts.copy()
    value = job()
    for _ in range(20):
        assert owner.validate_document(owner.JOB, value) == value
    assert counts['build'] == 1
    assert all(counts[key] - before[key] == 20 for key in ['tree', 'match', 'semantic'])
    value['payload']['question'] = 'Different original text'
    with pytest.raises(owner.WorkflowContractError):
        owner.validate_document(owner.JOB, value)
    value = job()
    value['budgets']['extra'] = True
    with pytest.raises(owner.WorkflowContractError):
        owner.validate_document(owner.JOB, value)
    assert counts['build'] == 1


def test_result_matcher_adjustment_does_not_mutate_cached_public_number_shape():
    before = owner.schema_document(owner.RESULT)
    value = result()
    value['observed_usage']['seconds'] = 0.25
    assert owner.validate_document(owner.RESULT, value) == value
    assert owner.schema_document(owner.RESULT) == before
    assert before['properties']['observed_usage']['properties']['seconds']['anyOf'][0]['type'] == 'number'


def test_warm_schema_cache_does_not_bypass_live_deadline_or_stored_correlation():
    parent = job()
    assert owner.validate_job(parent, now_utc=NOW) == parent
    with pytest.raises(owner.WorkflowContractError) as exc:
        owner.validate_job(parent, now_utc=DEADLINE)
    assert exc.value.error_code == 'WORKFLOW_DEADLINE_EXPIRED'
    followup = job('poll')
    assert owner.validate_job(followup, now_utc=NOW, parent_job=parent) == followup
    changed = copy.deepcopy(parent)
    changed['basis_id'] = 'different-original-basis'
    with pytest.raises(owner.WorkflowContractError) as exc:
        owner.validate_job(followup, now_utc=NOW, parent_job=changed)
    assert exc.value.error_code == 'WORKFLOW_CORRELATION_MISMATCH'
    received = result(parent)
    received['controller_ref'] = 'other-controller'
    with pytest.raises(owner.WorkflowContractError) as exc:
        owner.validate_result(received, job=parent, now_utc=NOW)
    assert exc.value.error_code == 'WORKFLOW_CORRELATION_MISMATCH'


@pytest.mark.parametrize('name', ['unknown/v1', None, 0, ['research-host-job/v1']])
def test_warm_schema_catalog_keeps_unknown_or_nonstring_selection_closed(name):
    owner.schema_catalog()
    with pytest.raises(owner.WorkflowContractError) as exc:
        owner.schema_document(name)
    assert exc.value.error_code == 'WORKFLOW_VERSION_UNSUPPORTED'
