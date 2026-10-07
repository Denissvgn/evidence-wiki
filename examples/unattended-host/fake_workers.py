"""Deterministic synthetic workers: no credentials, held-out labels or live model."""

import copy
import json
import sys

CHECKS = ('support','source_suitability','scope','time','units','counterevidence')


def generate(data):
    original = data['original']
    source = data['sources'][0]
    quote = 'Measured solar reflectance was 0.74.'
    repaired = data['purpose'] == 'repair'
    text = 'Observed solar reflectance was 0.74.' if repaired else 'Solar reflectance is always 0.74.'
    claim = {'id':'observation','question_slug':original['questions'][0]['slug'],'qualification':'supported','text':text,
        'scope':'Synthetic local observation only.','time':'Captured observation only.','units':'Unitless reflectance.',
        'premises':[],'derivation':None,'limitations':['Fixture mechanics; not production review.'],
        'evidence':[{'source_id':source['source_id'],'record_sha256':source['record_sha256'],'observed_at':source['metadata']['host_capture']['retrieved_at'],
            'capture':'primary','quote':quote,'location_hint':None,'anchor':None}]}
    inventory = {'schema_version':'evidence-strict-claims/v1','questions':[{'slug':original['questions'][0]['slug'],
        'original_id':original['original_id'],'question':original['original_text'].strip()}],'claims':[claim]}
    if repaired:
        inventory = copy.deepcopy(data['repair_scope']['before_inventory'])
        next(row for row in inventory['claims'] if row['id']==data['repair_scope']['target_claim_id']).update(claim)
    proposal = {'schema_version':'evidence-draft-proposal/v1','originals':[{'original_id':original['original_id'],
        'text':original['original_text'],'question_slugs':[original['questions'][0]['slug']]}],'inventory':inventory}
    return {'proposal':proposal,'coverage_links':[] if repaired else [{'slug':original['questions'][0]['slug'],
        'facet_id':'primary','source_ids':[source['source_id']]}]}


def main():
    envelope = json.load(sys.stdin)
    data = envelope['input']
    if 'case_id' in data:
        result = {'case_id':data['case_id'],'label':'accept' if data['supported'] else 'reject'}
    elif 'owner_actions' in data:
        result = {'schema_version':'research-work-proposal/v1','basis_id':data['basis_id'],
            'candidates':[dict(data['owner_actions'][0],candidate_id='next',rationale='Exact permitted owner action')]}
    elif sys.argv[1] in {'generator','repairer'}:
        result = generate(data)
    else:
        result = {'verdicts':dict.fromkeys(CHECKS,'pass'),
            'rationale':'A single retained measurement cannot establish a universal value.'}
        if sys.argv[1]=='critic' and ('always' in data['packet']['claim']['text'] or sys.argv[2]=='abstain'):
            result['verdicts'].update(support='fail',scope='fail')
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))


if __name__=='__main__':
    main()
