"""Unattended public-API host with isolated fixture and explicitly provisioned live modes."""

import argparse
import contextlib
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evidence_wiki import HostRuntime, OperationControl, Workspace
from evidence_wiki.errors import EvidenceWikiError

MAX_OUTPUT = 1_048_576


def load_services(path, root):
    supplied = Path(path).expanduser().absolute()
    if supplied.is_symlink() or supplied.resolve() != supplied:
        raise ValueError('Live service factory must be a canonical owner-private file.')
    descriptor = os.open(supplied,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 65_536:
            raise ValueError('Live service factory must be an owner-private regular file, at most 64 KiB.')
        data = b''
        while chunk := os.read(descriptor,min(65536,65537-len(data))):
            data += chunk
            if len(data)>65536:
                raise ValueError('Service factory exceeds byte bound.')
        fields = ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
        frozen = tuple(getattr(info,name) for name in fields)
        if any(tuple(getattr(value,name) for name in fields)!=frozen for value in (os.fstat(descriptor),supplied.stat())):
            raise ValueError('Service factory changed during selection.')
    finally:
        os.close(descriptor)
    namespace = {'__name__':'selected_live_host_services','__file__':str(supplied)}
    try:
        exec(compile(data,str(supplied),'exec'),namespace)  # noqa: S102 - explicit owner-private trusted host code, never worker input.
        runtime = namespace['build_runtime'](root)
    except (Exception,SystemExit):
        raise ValueError('Selected host services could not be loaded.') from None
    if type(runtime) is not HostRuntime:
        raise ValueError('The trusted factory must return public HostRuntime services.')
    return runtime


def live_runtime(args):
    if not args.workspace or bool(args.runtime) == bool(args.services):
        raise ValueError('Live mode requires --workspace and exactly one explicit --runtime or --services selection.')
    root = Path(args.workspace).expanduser().resolve(strict=True)
    runtime = HostRuntime.from_file(args.runtime) if args.runtime else load_services(args.services,root)
    if any(role.model_ref.startswith(('simulated/','offline/','fixture/')) for role in runtime.execution.profile.roles):
        raise ValueError('Fixture/offline service identities cannot be promoted into live mode.')
    return root,runtime,{'setup_ready':'host-provisioned','source_admitted':'native-current-checks','scenario':'live'}


def execute(root, runtime, *, run_id, request_id, max_steps, refresh=True, refresh_max_steps=8, fixture=False):
    seconds = min(1800,runtime.policy.to_dict()['budgets']['seconds'])
    control = OperationControl(deadline_utc=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),timeout_seconds=seconds)
    reports = []
    try:
        with runtime.context.activate(), Workspace.open(root) as workspace:
            cycle = workspace.research.run_cycle(runtime.policy,run_id=run_id,execution=runtime.execution,
                request_id=request_id,max_steps=max_steps,operation_control=control)
            reports.append(cycle)
            with workspace.research.session(runtime.policy,run_id=run_id,execution=runtime.execution) as session:
                brief = session.brief()
                accounting = session.report(max_records=64)
            if refresh and cycle['outcome'] in {'accepted','partial'}:
                profiles = runtime.execution.profile.role_scopes
                planners = [role.config_ref for role in runtime.execution.profile.roles
                    if profiles and profiles.scope(role.config_ref).task_role=='planner']
                try:
                    refreshed = workspace.research.refresh(runtime.policy,run_id=run_id+'-refresh',previous_run_id=run_id,
                        execution=runtime.execution,request_id=request_id+'-refresh',planner_config_ref=planners[0] if len(planners)==1 else None,
                        max_steps=refresh_max_steps,operation_control=control)
                    reports.append(refreshed)
                except EvidenceWikiError as error:
                    reports.append({'stage':'refresh','terminal':True,'status':'refused','error_code':error.error_code,
                        'authorized':False,'evidence_accepted':False})
        return {'schema_version':'unattended-host-result/v1','observation_class':'fixture_mechanics' if fixture else 'live_host_execution',
            'production_qualification':False if fixture else 'native_current_owners_only','reports':reports,'brief':brief,
            'accounting':accounting,'authorized':False,'evidence_accepted':False}
    finally:
        control.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('fake','live'),required=True)
    parser.add_argument('--output',help='New fixture directory; never a production workspace.')
    parser.add_argument('--workspace',help='Existing live host-provisioned workspace.')
    parser.add_argument('--runtime',help='Explicit owner-private runtime JSON (native supported file profile).')
    parser.add_argument('--services',help='Explicit trusted owner-private Python build_runtime(root) factory, for complete public services.')
    parser.add_argument('--scenario',choices=('repair','abstain'),default='repair')
    parser.add_argument('--run-id',default='study')
    parser.add_argument('--request-id',default='drive')
    parser.add_argument('--max-steps',type=int,default=64)
    parser.add_argument('--refresh-max-steps',type=int,default=8)
    parser.add_argument('--no-refresh',action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.max_steps <= 64 or not 1 <= args.refresh_max_steps <= 64:
        parser.error('Step bounds must be between 1 and 64')
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if args.mode=='fake':
                if not args.output or args.runtime or args.services or args.workspace:
                    raise ValueError('Fake mode requires a new --output and accepts no live services.')
                from fixture_host import provision
                root,runtime,setup = provision(Path(args.output),'What observation was retained?',scenario=args.scenario)
            else:
                root,runtime,setup = live_runtime(args)
            result = execute(root,runtime,run_id=args.run_id,request_id=args.request_id,max_steps=args.max_steps,
                refresh=not args.no_refresh,refresh_max_steps=args.refresh_max_steps,fixture=args.mode=='fake')
            result['setup'] = setup
        rendered = json.dumps(result,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        if len(rendered.encode())+1 > MAX_OUTPUT:
            raise ValueError('Result exceeds the finite output bound; use smaller native pages.')
        print(rendered)
        return 0 if result['reports'][0]['outcome'] in {'accepted','partial'} else 3
    except EvidenceWikiError as error:
        print(json.dumps({'schema_version':'unattended-host-result/v1','terminal':True,'status':'refused',
            'observation_class':'fixture_mechanics' if args.mode=='fake' else 'live_host_execution',
            'error':{'error_code':error.error_code,'message':error.message,'details':error.details},
            'authorized':False,'evidence_accepted':False},separators=(',',':')))
        return error.exit_code
    except (ValueError,OSError,KeyError,TypeError):
        print(json.dumps({'schema_version':'unattended-host-result/v1','terminal':True,'status':'refused',
            'observation_class':'fixture_mechanics' if args.mode=='fake' else 'live_host_execution',
            'error':{'error_code':'HOST_CONFIGURATION_REFUSED','message':'Explicit host configuration is unavailable or invalid; no credentials are provisioned.'},
            'authorized':False,'evidence_accepted':False},separators=(',',':')))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
