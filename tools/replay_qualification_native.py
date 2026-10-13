#!/usr/bin/env python3
"""Recheck retained native qualification through public installed services."""

import argparse
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import evidence_wiki
from evidence_wiki import HostRuntime, Workspace, contract, host_qualification

PART_BYTES = 32768
PAYLOAD_BYTES = 4_194_304


def _artifact_bytes(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)+'\n').encode()


def _private_part(path, maximum, *, directory_fd=None):
    descriptor = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o077 or before.st_size > maximum):
            raise ValueError('Native proof artifact is not private bounded regular data.')
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor,min(65536,maximum+1-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named = os.stat(path,dir_fd=directory_fd,follow_symlinks=False)
        fields = ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_uid','st_mode','st_nlink')
        if (len(raw) != before.st_size or len(raw) > maximum
                or any(getattr(before,key) != getattr(after,key) or getattr(after,key) != getattr(named,key) for key in fields)):
            raise ValueError('Native proof artifact changed during read.')
        return bytes(raw)
    finally:
        os.close(descriptor)


@contextmanager
def _artifact_directory(directory, *, writing=False):
    """Keep every named ancestor and the private directory anchored across parts."""
    directory = Path(directory).absolute()
    if '..' in directory.parts:
        raise ValueError('Native proof path is not canonical.')
    held = []
    try:
        held.append(('/',os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)))
        for name in directory.parts[1:]:
            held.append((name,os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=held[-1][1])))
        initial = os.fstat(held[-1][1])
        if initial.st_uid != os.geteuid() or initial.st_mode&0o077:
            raise ValueError('Native proof directory is not canonical private custody.')
        yield held[-1][1]
        final = os.fstat(held[-1][1])
        for field in (('st_dev','st_ino','st_uid','st_mode') if writing else
                      ('st_dev','st_ino','st_uid','st_mode','st_nlink','st_mtime_ns','st_ctime_ns')):
            if getattr(initial,field) != getattr(final,field):
                raise ValueError('Native proof directory changed during read.')
        for index,(name,descriptor) in enumerate(held[1:],1):
            actual = os.fstat(descriptor)
            named = os.stat(name,dir_fd=held[index-1][1],follow_symlinks=False)
            if (not stat.S_ISDIR(named.st_mode) or any(getattr(actual,key) != getattr(named,key)
                    for key in ('st_dev','st_ino','st_uid','st_mode'))):
                raise ValueError('Native proof ancestor changed during read.')
    finally:
        for _name,descriptor in reversed(held):
            os.close(descriptor)


def write_artifact(result, directory):
    """Retain the complete owner-produced result without enlarging stdout."""
    raw = _artifact_bytes(result)
    if not raw or len(raw) > PAYLOAD_BYTES:
        raise ValueError('Complete native proof exceeds the existing aggregate bound.')
    directory = Path(directory).absolute()
    if directory.resolve() != directory:
        raise ValueError('Native proof artifact path is not canonical.')
    directory.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    directory.mkdir(mode=0o700,exist_ok=False)
    with _artifact_directory(directory,writing=True) as directory_fd:
        parts = []
        for index, start in enumerate(range(0,len(raw),PART_BYTES)):
            piece = raw[start:start+PART_BYTES]
            name = f'part-{index:04d}.bin'
            descriptor = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(piece)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(),0o400)
            parts.append({'name':name,'bytes':len(piece),'sha256':hashlib.sha256(piece).hexdigest()})
        manifest = {'schema_version':'qualification-native-artifact/v1','payload_bytes':len(raw),
            'payload_sha256':hashlib.sha256(raw).hexdigest(),'parts':parts}
        encoded = _artifact_bytes(manifest)
        if len(encoded) > PART_BYTES:
            raise ValueError('Native proof manifest exceeds the existing record bound.')
        descriptor = os.open('manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory_fd)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(),0o400)
        os.fsync(directory_fd)
    return {'schema_version':'qualification-native-artifact-output/v1',
        'manifest':{'path':str(directory/'manifest.json'),'sha256':hashlib.sha256(encoded).hexdigest(),'bytes':len(encoded)}}


def _read_artifact(summary, directory, directory_fd):
    """Join every original private part to the signed stdout commitment."""
    directory = Path(directory).absolute()
    if (type(summary) is not dict or set(summary) != {'schema_version','manifest'}
            or summary['schema_version'] != 'qualification-native-artifact-output/v1'):
        raise ValueError('Native proof output is not the complete artifact commitment.')
    ref = summary['manifest']
    if type(ref) is not dict or set(ref) != {'path','sha256','bytes'} or ref['path'] != str(directory/'manifest.json'):
        raise ValueError('Native proof manifest path differs from the frozen route.')
    raw = _private_part('manifest.json',PART_BYTES,directory_fd=directory_fd)
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('Original native proof manifest changed.')
    manifest = json.loads(raw)
    if (type(manifest) is not dict or set(manifest) != {'schema_version','payload_bytes','payload_sha256','parts'}
            or manifest['schema_version'] != 'qualification-native-artifact/v1'
            or type(manifest['payload_bytes']) is not int or not 0 < manifest['payload_bytes'] <= PAYLOAD_BYTES
            or type(manifest['parts']) is not list or len(manifest['parts']) != (manifest['payload_bytes']+PART_BYTES-1)//PART_BYTES):
        raise ValueError('Native proof complete part inventory is invalid.')
    expected_names = [f'part-{index:04d}.bin' for index in range(len(manifest['parts']))]
    if (set(os.listdir(directory_fd)) != {'manifest.json',*expected_names}
            or [row.get('name') for row in manifest['parts']] != expected_names):
        raise ValueError('Native proof parts are missing, extra, duplicated or reordered.')
    output = bytearray()
    for index, item in enumerate(manifest['parts']):
        if type(item) is not dict or set(item) != {'name','bytes','sha256'}:
            raise ValueError('Native proof part shape is invalid.')
        piece = _private_part(item['name'],PART_BYTES,directory_fd=directory_fd)
        expected_size = min(PART_BYTES,manifest['payload_bytes']-index*PART_BYTES)
        if len(piece) != expected_size or item['bytes'] != expected_size or hashlib.sha256(piece).hexdigest() != item['sha256']:
            raise ValueError('Original native proof part changed or was truncated.')
        output.extend(piece)
    if len(output) != manifest['payload_bytes'] or hashlib.sha256(output).hexdigest() != manifest['payload_sha256']:
        raise ValueError('Complete original native proof payload differs.')
    value = json.loads(output)
    if _artifact_bytes(value) != output:
        raise ValueError('Native proof payload is not exact canonical data.')
    if set(os.listdir(directory_fd)) != {'manifest.json',*expected_names}:
        raise ValueError('Native proof directory inventory changed.')
    return value


def read_artifact(summary, directory):
    with _artifact_directory(directory) as descriptor:
        return _read_artifact(summary,directory,descriptor)


def journal_snapshot(session, records):
    """Capture the entire event source of the producer-authenticated chain."""
    directory = Path(session.root)/'runs'/session.run_id
    raw = _private_part(directory/'events.jsonl',PAYLOAD_BYTES)
    events = [json.loads(line) for line in raw.splitlines()]
    retained = [{**event['data']['record'],'event_ref':event['event_id']} for event in events
        if 'record' in (event.get('data') or {})]
    if retained != records:
        raise ValueError('Native journal changed after original owner authentication.')
    state_raw = _private_part(directory/'run-state.json',PART_BYTES*8)
    return {'run_id':session.run_id,'events':events,'state':json.loads(state_raw),
        'source_path':str(directory/'events.jsonl'),'source_sha256':hashlib.sha256(raw).hexdigest(),
        'state_path':str(directory/'run-state.json'),'state_sha256':hashlib.sha256(state_raw).hexdigest(),
        'original_state_json':state_raw.decode()}


def require_current_probe(records, current, finished_utc):
    probes = [row['data']['probe'] for row in records if row['kind'] == 'execution_qualification_probe']
    if not probes or current['qualification']['state'] != 'qualified':
        raise ValueError('Current native qualification is unavailable.')
    probe = probes[-1]
    if (probe['reasons'] or probe.get('configuration_reasons')
            or not datetime.fromisoformat(probe['observed_utc']) <= datetime.fromisoformat(finished_utc)
            < datetime.fromisoformat(probe['expires_utc'])):
        raise ValueError('Current native closing observation outlived its original witness.')


def verify_journal_closing(snapshots):
    for snapshot in snapshots:
        if (hashlib.sha256(_private_part(snapshot['source_path'],PAYLOAD_BYTES)).hexdigest() != snapshot['source_sha256']
                or hashlib.sha256(_private_part(snapshot['state_path'],PART_BYTES*8)).hexdigest() != snapshot['state_sha256']):
            raise ValueError('Native main or additional journal changed before closing.')


def checked_reference_inventory(runtime, rows, issued_records):
    """Join every active reference obligation to its original issued receipt."""
    from evidence_wiki.qualification_calibration import digest
    from evidence_wiki.reference_oracles import protected_cases

    profile = runtime.execution.profile
    cases = protected_cases()
    lookup = {case['case_id']:case for case in cases}
    selections = {selection.config_ref:selection for selection in profile.calibration.selections}
    if len(selections) != 4 or len(lookup) != len(cases):
        raise ValueError('The supported reference configuration/case inventory differs.')
    records = {}
    for supplied in issued_records:
        record = runtime.execution.qualifications.authority.verify(supplied)
        config = record['binding']['config_ref']
        if config not in selections or config in records:
            raise ValueError('Reference qualification configuration inventory differs.')
        selected = selections[config].document()
        if (selected['reference_assessment']['cases_sha256'] != digest(cases)
                or record['suite_sha256'] != selected['suite_sha256']
                or record['binding']['policy_id'] != runtime.policy.policy_id
                or record['binding']['execution_profile_sha256'] != profile.sha256):
            raise ValueError('Reference qualification differs from the active profile.')
        refs = record['observation_refs']
        if len(refs) != len(lookup) or {item['case_id'] for item in refs} != set(lookup):
            raise ValueError('Issued reference obligation inventory is incomplete or duplicated.')
        records[config] = {item['case_id']:item for item in refs}
    if set(records) != set(selections):
        raise ValueError('Required reference qualifications are missing.')
    expected = {(config,case_id) for config in selections for case_id in lookup}
    reservations, observed, finals, completed = {}, {}, {}, set()
    for row in rows:
        document, supplied = row['attempt'], row['observation']
        identifier = document['attempt_id']
        if supplied['state'] == 'reserved':
            if identifier in reservations or identifier in observed:
                raise ValueError('Reference reservation is duplicated or misordered.')
            reservations[identifier] = document
            continue
        if supplied['state'] == 'dispatch_unknown':
            raise ValueError('Reference dispatch remains unknown.')
        if identifier not in reservations or reservations[identifier] != document or identifier in completed:
            raise ValueError('Reference observation has no exact original reservation or follows completion.')
        attempt = runtime.execution.attempt(document)
        receipt = runtime.execution.executor.authenticate(supplied,attempt)
        prior = observed.get(identifier)
        if prior is not None and receipt['sequence'] <= prior['sequence']:
            raise ValueError('Reference observation is duplicated or regresses.')
        observed[identifier] = receipt
        if receipt['final_accounting'] is not True:
            continue
        config, case_id = receipt['config_ref'], receipt['output']['case_id']
        key = (config,case_id)
        if key not in expected or key in finals:
            raise ValueError('Reference configuration/case is extra or duplicated.')
        issued = records[config][case_id]
        if (attempt.config_ref != config or attempt.input_document != lookup[case_id]['input_document']
                or attempt.job['operation'] != 'evaluate' or attempt.job['policy_id'] != runtime.policy.policy_id
                or receipt['state'] != 'succeeded' or receipt['started_utc'] is None
                or issued != {'case_id':case_id,'attempt_id':identifier,
                    'observation_sha256':digest(receipt),'input_sha256':attempt.input_sha256}):
            raise ValueError('Reference final differs from the exact issued case/input/attempt receipt.')
        finals[key] = {'attempt_id':identifier,'case_id':case_id,'config_ref':config,
            'started_utc':receipt['started_utc'],'state':receipt['state'],'final_accounting':True,
            'receipt_sha256':digest(receipt),'input_sha256':attempt.input_sha256}
        completed.add(identifier)
    if set(finals) != expected or set(reservations) != completed or set(observed) != completed:
        raise ValueError('Active reference attempt inventory is incomplete.')
    return [finals[key] for key in sorted(expected)]


def _stable_current(current):
    result = deepcopy({name:current[name] for name in ('basis_id','policy_id','accounting','canonical_release','qualification')})
    publication = result['canonical_release'].get('publication')
    if type(publication) is dict and 'evaluated_at' in publication:
        clock = publication['evaluated_at']
        if type(clock) is not str or datetime.fromisoformat(clock).tzinfo is None:
            raise ValueError('Native current publication observation clock is invalid.')
        publication['evaluated_at'] = 'actual-owner-observation-clock'
    return result


def closing_qualification(owner, session, current, before, after):
    """Keep the real owner epoch and recompute live eligibility at closing."""
    if before != after:
        raise ValueError('Native stable source hashes changed during closing capture.')
    fresh = owner.current_qualifications(session)
    if fresh != current['qualification'] or fresh['state'] != 'qualified':
        raise ValueError('Native current qualification changed during closing capture.')
    closing = session.current()
    original, final = _stable_current(current), _stable_current(closing)
    if original != final:
        fields = [name for name in original if original[name] != final[name]]
        raise ValueError('Native closing current changed: '+','.join(fields))
    return closing


def _host_files(root, private, control, live_paths):
    return {str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest()
        for base in (Path(root),private,Path(control)) for path in base.rglob('*')
        if path.is_file() and not path.is_symlink() and str(path.resolve()) not in live_paths
        and not any(part in {'__pycache__','.locks'} for part in path.parts)}


def _native_receipts(runtime, records):
    attempts = [row['data']['attempt'] for row in records if row['kind'] == 'execution_attempt']
    receipts = {}
    for row in records:
        if row['kind'] == 'execution_observation':
            receipt = row['data']['observation']
            attempt = next(item for item in attempts if item['attempt_id'] == receipt['attempt_id'])
            receipts[receipt['attempt_id']] = runtime.execution.executor.authenticate(receipt,runtime.execution.attempt(attempt))
    if any(receipts.get(item['attempt_id'],{}).get('final_accounting') is not True for item in attempts):
        raise ValueError('Retained native attempt accounting is incomplete.')
    return attempts,list(receipts.values())


def additional_snapshot(runtime, child, owner):
    current,observed,_packets,records = owner._observe_current(child)
    _attempts,receipts = _native_receipts(runtime,records)
    result = {'run_id':child.run_id,'current':current,
        'records_sha256':hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'native_receipts':receipts,'journal':journal_snapshot(child,records)}
    observed.finish()
    if runtime.execution.records(child) != records:
        raise ValueError('Native additional current and journal changed during capture.')
    return result


def replay(root, factory, run_id, additional_run_ids=(), *, expected_control_root=None):
    import secrets

    from evidence_wiki import BoundedSubprocessExecutor, execution_session
    from evidence_wiki.calibration_probes import refresh

    runtime = HostRuntime.from_factory(factory,root)
    if expected_control_root is not None:
        expected = Path(expected_control_root)
        if (expected != Path(root).parent / 'fixture-control' or expected.resolve(strict=True) != expected
                or runtime.context.mutation_control_directory != expected):
            raise ValueError('integration_current_binding_changed')
    if type(runtime.execution.executor) is not BoundedSubprocessExecutor:
        raise ValueError('The fixed native producer requires its supported bounded local host.')
    private = Path(factory).parent
    identifiers = [run_id,*additional_run_ids]
    live_paths = {str(Path(root)/'runs'/identifier/name) for identifier in identifiers for name in ('events.jsonl','run-state.json')}
    with runtime.context.activate(),Workspace.open(root) as workspace:
        with workspace.research.session(runtime.policy,run_id=run_id,execution=runtime.execution) as session:
            reference_rows = [json.loads(line) for line in (private/'reference-observations.jsonl').read_bytes().splitlines()]
            issued_records = json.loads((private/'finite-services.json').read_bytes())['qualification_records']
            references = checked_reference_inventory(runtime,reference_rows,issued_records)
            # All protected reference work precedes this genuine owner callback.
            refresh(session,'native-witness-'+secrets.token_hex(16))
            started = datetime.now(timezone.utc).isoformat()
            # Only the exact local native HMAC host is consumed under this gate.
            # Provider callbacks and reference issuance are never inside it.
            with execution_session.gate(session,'resolution'):
                before = _host_files(root,private,runtime.context.mutation_control_directory,live_paths)
                with execution_session._current_facts(session) as facts:
                    current,records = facts.view,facts.records
                    qualification = host_qualification(session)
                    attempts,receipts = _native_receipts(runtime,records)
                    if (not attempts or current['accounting']['accepted_count'] != 1
                            or not current['canonical_release']['research_complete']):
                        raise ValueError('Current native finite result is not accepted.')
                    profile = runtime.execution.profile
                    if profile.document()['schema_version'] != 'research-execution-profile/v13':
                        raise ValueError('The current finite semantic profile is unavailable.')
                    package = Path(evidence_wiki.__file__).resolve().parent
                    files = {path.relative_to(package).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(package.rglob('*')) if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
                    result = {'schema_version':'qualification-native-replay/v1','package_origin':str(package),
                        'interpreter':sys.executable,'prefix':sys.prefix,'package_files':files,
                        'api_version':contract()['library_api']['version'],'policy':runtime.policy.to_dict(),
                        'profile':{'schema_version':profile.document()['schema_version'],'profile_sha256':profile.sha256,
                            'profile_ref':profile.profile_ref,'probe_lifetime_seconds':profile.calibration.probe_lifetime_seconds,
                            'calibration':[{'config_ref':selection.config_ref,'selection_sha256':selection.sha256,
                                **{name:selection.document()[name] for name in ('model_revision','model_settings','rubric_sha256','suite_sha256','split_ref')},
                                'domain_descriptor_sha256':hashlib.sha256(json.dumps(selection.document()['domain_descriptor'],ensure_ascii=False,
                                    sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest(),
                                'reference_assessment_ref':selection.document()['reference_assessment']['assessment_ref']}
                                for selection in profile.calibration.selections]},
                        'qualification':qualification,'current':current,
                        'records_sha256':hashlib.sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                        'native_receipts':receipts,'factory_sha256':hashlib.sha256(Path(factory).read_bytes()).hexdigest(),
                        'reference_attempts':references,'workspace':str(Path(root).resolve()),'run_id':run_id,
                        'observation_class':'fixture_mechanics','journal':journal_snapshot(session,records),'read_started_utc':started}
                    additional = []
                    for identifier in additional_run_ids:
                        with workspace.research.session(runtime.policy,run_id=identifier,execution=runtime.execution) as child:
                            additional.append(additional_snapshot(runtime,child,execution_session))
                    result['additional_runs'] = additional
                    after = _host_files(root,private,runtime.context.mutation_control_directory,live_paths)
                    result['current'] = closing_qualification(execution_session,session,current,before,after)
                    verify_journal_closing([result['journal'],*(item['journal'] for item in additional)])
                    result['host_inputs'] = after
                result['read_finished_utc'] = datetime.now(timezone.utc).isoformat()
                require_current_probe(records,current,result['read_finished_utc'])
                if profile.calibration.probe_lifetime_seconds != 30:
                    raise ValueError('Native current witness lifetime differs from the frozen profile.')
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True)
    parser.add_argument('--services',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--additional-run-id',action='append',default=[])
    parser.add_argument('--artifact-dir',required=True)
    args = parser.parse_args(argv)
    result = replay(args.workspace,args.services,args.run_id,args.additional_run_id)
    print(json.dumps(write_artifact(result,args.artifact_dir),sort_keys=True,separators=(',',':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
