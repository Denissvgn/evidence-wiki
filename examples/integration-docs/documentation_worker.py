"""Scripted canonical document assembly and independent consistency mechanics."""

import json
import sys


def main():
    document=json.load(sys.stdin);packet=document['input']
    if document['job']['task_role']=='integration':
        output={'schema_version':packet['proposal_template']['schema_version'],'basis_id':packet['basis_id'],
            'section_ids':packet['reference']['section_order'],
            'operation_ids':[row['operation'] for row in packet['reference']['operations']]}
        if packet.get('scenario')=='always-fail' or packet.get('scenario')=='repair' and packet['attempt']==1:
            output['section_ids']=[]
    else:
        output={'schema_version':'integration-documentation-critique/v1','basis_id':packet['basis_id'],
            'verdict':'fail' if packet['native_issues'] else 'pass'}
    print(json.dumps(output))


if __name__=='__main__':
    main()
