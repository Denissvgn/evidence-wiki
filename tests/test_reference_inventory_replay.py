"""Exact original reference joins reject omitted or substituted obligations."""

import copy
from types import SimpleNamespace

import pytest

from evidence_wiki.qualification_calibration import digest
from tools.replay_qualification_native import checked_reference_inventory


def fixture(monkeypatch):
    cases = [{'case_id':name,'input_document':{'value':index}} for index,name in enumerate(('one','two'))]
    monkeypatch.setattr('evidence_wiki.reference_oracles.protected_cases',lambda:copy.deepcopy(cases))
    configs = ('evidence','scope','critic','adjudicator')
    selections = [SimpleNamespace(config_ref=config,document=lambda:{'suite_sha256':'suite',
        'reference_assessment':{'cases_sha256':digest(cases)}}) for config in configs]
    profile = SimpleNamespace(sha256='profile',calibration=SimpleNamespace(selections=selections))
    rows,records = [],[]
    for config in configs:
        refs = []
        for case in cases:
            identifier = config+'-'+case['case_id']
            attempt = {'attempt_id':identifier,'config_ref':config,'input_document':case['input_document'],
                'input_sha256':digest(case['input_document']),'job':{'operation':'evaluate','policy_id':'policy'}}
            initial = {'attempt_id':identifier,'config_ref':config,'sequence':1,'state':'running','final_accounting':False}
            final = {'attempt_id':identifier,'config_ref':config,'sequence':2,'state':'succeeded','final_accounting':True,
                'started_utc':'2026-10-01T00:00:00+00:00','output':{'case_id':case['case_id'],'label':'accept'}}
            rows.extend([{'attempt':attempt,'observation':{'state':'reserved'}},
                {'attempt':attempt,'observation':initial},{'attempt':attempt,'observation':final}])
            refs.append({'case_id':case['case_id'],'attempt_id':identifier,'observation_sha256':digest(final),
                'input_sha256':attempt['input_sha256']})
        records.append({'binding':{'config_ref':config,'policy_id':'policy','execution_profile_sha256':'profile'},
            'suite_sha256':'suite','observation_refs':refs})
    runtime = SimpleNamespace(policy=SimpleNamespace(policy_id='policy'),execution=SimpleNamespace(profile=profile,
        qualifications=SimpleNamespace(authority=SimpleNamespace(verify=lambda value:copy.deepcopy(value))),
        attempt=lambda value:SimpleNamespace(**value),executor=SimpleNamespace(authenticate=lambda receipt,_attempt:copy.deepcopy(receipt))))
    return runtime,rows,records


def test_all_active_config_cases_join_exact_original_issued_receipts(monkeypatch):
    runtime,rows,records = fixture(monkeypatch)
    result = checked_reference_inventory(runtime,rows,records)
    assert len(result) == 8 and len({(row['config_ref'],row['case_id']) for row in result}) == 8
    assert all(row['final_accounting'] for row in result)


@pytest.mark.parametrize('mutation',['omit','extra','duplicate','config','input','issued','record','reservation'])
def test_nonempty_reference_inventory_cannot_hide_missing_extra_or_rebound_case(monkeypatch,mutation):
    runtime,rows,records = fixture(monkeypatch)
    if mutation == 'omit':
        rows = rows[3:]
    elif mutation == 'extra':
        extra = copy.deepcopy(rows[:3])
        for row in extra:row['attempt']['attempt_id']='extra'
        rows.extend(extra)
    elif mutation == 'duplicate':
        rows.append(copy.deepcopy(rows[-1]))
    elif mutation == 'config':
        rows[-1]['observation']['config_ref']='evidence'
    elif mutation == 'input':
        rows[-1]['attempt']['input_document']={'value':99}
    elif mutation == 'issued':
        records[-1]['observation_refs'][-1]['observation_sha256']='different'
    elif mutation == 'record':
        records[-1]['observation_refs'].pop()
    else:
        rows.pop(0)
    with pytest.raises(ValueError):
        checked_reference_inventory(runtime,rows,records)
