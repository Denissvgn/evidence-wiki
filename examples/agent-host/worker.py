"""Deterministic instruction-selection mechanics; never a semantic reviewer."""

import json
import sys

document = json.load(sys.stdin)
packet = document['input']
if document['job']['task_role']=='planner':
    identifiers = packet['required_resource_ids']
    if sys.argv[1:]==['omit']:
        identifiers = []
    if sys.argv[1:]==['overload']:
        identifiers = [row['id'] for row in packet['resources']]
    output = {'schema_version':'agent-host-selection/v1','basis_id':packet['basis_id'],'resource_ids':identifiers}
else:
    missing = set(packet['required_resource_ids'])-set(packet['selected_resource_ids'])
    failed = bool(missing or packet['missing_capabilities'] or packet['native_reason']
                  or packet['context_bytes']>packet['max_context_bytes'])
    output = {'schema_version':'agent-host-critique/v1','basis_id':packet['basis_id'],'verdict':'fail' if failed else 'pass'}
print(json.dumps(output))
