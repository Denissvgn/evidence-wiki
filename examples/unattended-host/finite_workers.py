"""Scripted exact finite observations; no remote model or reference labels."""

import json
import sys

from fake_workers import generate

SOURCE = 'finite-structured-observation/v1'


def main():
    data = json.load(sys.stdin)['input']
    if data.get('schema_version') == 'finite-reference-input/v1':
        from evidence_wiki.reference_oracles import reproduce_label
        result = {'case_id': sys.argv[2], 'label': reproduce_label(data)}
    elif 'owner_actions' in data:
        result = {'schema_version':'research-work-proposal/v1','basis_id':data['basis_id'],
            'candidates':[dict(data['owner_actions'][0],candidate_id='next',rationale='Exact permitted finite fixture owner action')]}
    elif sys.argv[1] == 'generator':
        result = generate(data)
        source = next(json.loads(line) for line in data['sources'][0]['content'].splitlines()
            if line.startswith('{') and json.loads(line).get('schema_version') == SOURCE)
        claim = result['proposal']['inventory']['claims'][0]
        facts = source['facts']
        from fractions import Fraction
        left, right = (Fraction(value) for value in facts['values'])
        values = {'sum': lambda: left+right, 'difference': lambda: left-right, 'product': lambda: left*right,
            'mean': lambda: (left+right)/2, 'ratio': lambda: left/right}
        candidate = {name: facts[name] for name in ('scope','time','units')} | {'value': str(values[facts['operation']]())}
        quote = json.dumps({'schema_version': SOURCE, 'facts': facts},
            sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        claim.update(text='Finite observation: {operation}; value={value}; units={units}; scope={scope}; time={time}.'.format(operation=facts['operation'], **candidate),
            scope=candidate['scope'], time=candidate['time'], units=candidate['units'],
            limitations=['Synthetic finite declaration; source truth and broader prose remain unqualified.'])
        claim['evidence'][0]['quote'] = quote
    else:
        semantic = data['semantic']
        checks = {}
        for dimension, units in semantic['units'].items():
            rows = []
            for index, unit in enumerate(units):
                if dimension == 'support':
                    pointer, text = '/semantic/sources/0/content', semantic['sources'][0]['content']
                elif dimension == 'scoped_correctness':
                    pointer, text = '/packet/claim/text', data['packet']['claim']['text']
                elif dimension == 'missing_material_evidence':
                    pointer, text = '/semantic/required_units/'+str(index)+'/text', semantic['required_units'][index]['text']
                else:
                    pointer, text = '/semantic/original_text', semantic['original_text']
                rows.append({'unit_id': unit['unit_id'], 'verdict': 'pass' if unit['applicable'] else 'not_applicable',
                    'reason': 'Scripted exact finite fixture field.', 'severity': 'none', 'uncertainty': 'low',
                    'anchors': [{'pointer': pointer, 'start': 0, 'end': len(text), 'quote': text}] if unit['applicable'] else []})
            checks[dimension] = rows
        result = {'schema_version': 'research-semantic-adjudication/v1' if semantic['role'] == 'adjudicator' else 'research-semantic-findings/v1',
            'rationale': 'Synthetic finite fixture mechanics only.', 'checks': checks}
        if semantic['role'] != 'adjudicator':
            result.update(verdicts=dict.fromkeys(('support','source_suitability','scope','time','units','counterevidence'),'pass'), objections=[])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
