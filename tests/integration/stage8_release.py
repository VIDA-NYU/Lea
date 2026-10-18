"""Actual production role-image and binary rollback qualification.

No substituted image, source-code override, paid inference, registry write,
production mount or Docker-wide cleanup. All serving roles use their production
entrypoints and immutable image IDs. Test helpers enter only the private worker.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time
from urllib.request import Request,urlopen
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[2]
FIXTURE=ROOT/'tests/integration/stage8_release_fixture.py'
PYTHON='/app/adapter/.venv/bin/python'
ALIASES={'x86_64':'amd64','aarch64':'arm64','arm64':'arm64','amd64':'amd64'}


def docker(*args,timeout=240):
    result=subprocess.run(['docker',*map(str,args)],text=True,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError('Docker release probe failed: '+result.stderr+'\n'+result.stdout)
    return (result.stdout+result.stderr if args and args[0]=='logs' else result.stdout).strip()


def wait(fn,timeout=120):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            value=fn()
            if value:return value
        except OSError:pass
        time.sleep(.15)
    raise AssertionError('Release qualification condition timed out')


def request(base,path,body=None,headers=None):
    req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json',**(headers or {})})
    with urlopen(req,timeout=10) as response:return json.load(response)


def images(api,worker,companion,require_native=False):
    daemon=ALIASES.get(docker('info','--format','{{.Architecture}}'))
    host=ALIASES.get(platform.machine())
    result={}
    for role,name in [('api',api),('worker',worker),('companion',companion)]:
        info=json.loads(docker('image','inspect',name))[0]
        assert info['Os']=='linux' and info['Architecture'] in {'arm64','amd64'}
        if require_native:assert info['Architecture']==daemon==host,(role,info['Architecture'],daemon,host)
        result[role]={'image':name,'id':info['Id'],'architecture':info['Architecture'],'size_bytes':info['Size'],
            'source':(info.get('Config',{}).get('Labels') or {}).get('org.opencontainers.image.revision')}
    assert len({v['architecture'] for v in result.values()})==1
    architecture=result['worker']['architecture']
    binaries={}
    for role in ('worker','companion'):
        binaries[role+'_node']=json.loads(docker('run','--rm','--pull=never','--network','none','--entrypoint','node',
            result[role]['id'],'--print','JSON.stringify({architecture:process.arch,version:process.version})'))
        assert binaries[role+'_node']['architecture']=={'amd64':'x64','arm64':'arm64'}[architecture]
    binaries['api_python']=json.loads(docker('run','--rm','--pull=never','--network','none','--entrypoint',PYTHON,
        result['api']['id'],'-c','import json,platform,sqlite3;print(json.dumps(dict(architecture=platform.machine(),version=platform.python_version(),sqlite=sqlite3.sqlite_version)))'))
    assert ALIASES[binaries['api_python']['architecture']]==architecture
    return {'roles':result,'host_architecture':host,'docker_architecture':daemon,
        'native':all(v['architecture']==daemon==host for v in result.values()),
        'runner_arch':os.environ.get('RUNNER_ARCH'),'github_run_id':os.environ.get('GITHUB_RUN_ID'),'binaries':binaries}


class Stack:
    def __init__(self,home,bundle):
        self.home=home;self.bundle=bundle;self.created=[];self.names={};self.label='lea-release-'+uuid4().hex[:10]
        self.ownership_volume=self.label+'-ownership'
        docker('volume','create','--label','lea.stage8.release-fixture=true',self.ownership_volume)
        for name in ('data','config','proofs','projects','overleaf'):(home/name).mkdir(exist_ok=True)
        self.env=['-e','LEA_SHARED_DATA_DIR=/app/data','-e','LEA_MODEL_CATALOG_OFFLINE=true','-e','LITELLM_LOCAL_MODEL_COST_MAP=True',
            '-e','LEA_WORKSPACE_OWNERSHIP_DIR=/app/prover/workspace/.lea-runtime']

    def mounts(self,data='data'):
        return [part for source,target in [(data,'/app/data'),('config','/app/config'),('proofs','/app/prover/workspace/proofs'),
            ('projects','/app/prover/workspace/projects'),('overleaf','/app/apps/overleaf-extension/.overleaf-lean-stub'),
            ] for part in ('-v',f'{self.home/source}:{target}')]+['-v',self.ownership_volume+':/app/prover/workspace/.lea-runtime']

    def image(self,role):return self.bundle['roles'][role]['id']

    def private(self,mode,*,running=False):
        if running:
            # The helper itself is mounted at worker creation, never into API.
            output=docker('exec',self.names['worker'],PYTHON,'/validation/release.py',mode)
        else:
            output=docker('run','--rm','--pull=never','--network','none',*self.env,*self.mounts(),
                '-v',f'{FIXTURE}:/validation/release.py:ro','--entrypoint',PYTHON,self.image('worker'),'/validation/release.py',mode)
        return json.loads(output.splitlines()[-1])

    def seed(self,revision):
        docker('run','--rm','--pull=never','--network','none',*self.env,*self.mounts(),
            '-v',f'{ROOT/"tests/integration/container_seed.py"}:/validation/seed.py:ro','--entrypoint',PYTHON,self.image('worker'),'/validation/seed.py',revision)
        # Exercise a legacy local-user-owned DB, not just Docker-created files.
        docker('run','--rm','--pull=never','--network','none',*self.mounts(),'--entrypoint','chown',
            self.image('worker'),'-R',f'{os.getuid()}:{os.getgid()}','/app/data')
        docker('run','--rm','--pull=never','--network','none',*self.env,*self.mounts(),self.image('worker'),'migrate')
        # Prepare the other DB while no workspace writer exists. Migrations
        # themselves take the shared workspace ownership lock.
        (self.home/'second-data').mkdir()
        docker('run','--rm','--pull=never','--network','none',*self.env,*self.mounts('second-data'),self.image('worker'),'migrate')

    def start(self):
        api=self.label+'-api';worker=self.label+'-worker';companion=self.label+'-companion'
        docker('run','-d','--pull=never','--name',api,'--network','bridge','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges:true','--tmpfs','/tmp','-e','LEA_ALLOWED_HOSTS=api','-e','LEA_SHARED_DATA_DIR=/app/data',
            '-v',f'{self.home/"data"}:/app/data','-p','127.0.0.1::8001',self.image('api'),'api')
        self.created.append(api);self.names['api']=api;self.set_base()
        wait(lambda:request(self.base,'/api/health'))
        docker('run','-d','--pull=never','--name',worker,'--network','bridge','--link',api+':api',*self.env,
            '-e','LEA_API_BASE_URL=http://api:8001',*self.mounts(),'-v',f'{FIXTURE}:/validation/release.py:ro',self.image('worker'),'worker')
        self.created.append(worker);self.names['worker']=worker
        def worker_ready():
            status=docker('inspect','--format','{{.State.Status}}',worker)
            if status=='exited':raise RuntimeError('Production worker exited before readiness:\n'+docker('logs','--tail','60',worker))
            return request(self.base,'/api/health')['ready']
        wait(worker_ready,timeout=180)
        docker('run','-d','--pull=never','--name',companion,'--network','bridge','--link',api+':api','--read-only',
            '--cap-drop=ALL','--security-opt=no-new-privileges:true','--tmpfs','/tmp','-e','LEA_API_BASE_URL=http://api:8001',
            '-p','127.0.0.1::31245',self.image('companion'))
        self.created.append(companion);self.names['companion']=companion
        self.companion='http://'+docker('port',companion,'31245/tcp')
        wait(lambda:request(self.companion,'/health'))

    def set_base(self):self.base='http://'+docker('port',self.names['api'],'8001/tcp')

    def drain(self):
        request(self.base,'/api/runtime/restart',{'shutdown':True})
        assert wait(lambda:docker('inspect','--format','{{.State.Status}}',self.names['worker'])=='exited')
        assert docker('inspect','--format','{{.State.ExitCode}}',self.names['worker'])=='0'
        assert request(self.base,'/api/health')['ok']

    def stop(self):
        for name in reversed(self.created):docker('rm','-f',name)
        self.created=[];self.names={};self.label='lea-release-'+uuid4().hex[:10]

    def restore_ownership(self):
        docker('run','--rm','--pull=never','--network','none','-v',f'{self.home}:/fixture','--entrypoint','chown',
            self.image('worker'),'-R',f'{os.getuid()}:{os.getgid()}','/fixture')

    def cleanup(self):
        try:
            self.stop()
            self.restore_ownership()
        finally:docker('volume','rm',self.ownership_volume)


@contextmanager
def fixture(bundle):
    with tempfile.TemporaryDirectory(prefix='lea-stage8-release-') as folder:
        stack=Stack(Path(folder),bundle)
        try:yield stack
        except BaseException:
            for name in stack.created:print(docker('logs','--tail','60',name))
            raise
        finally:stack.cleanup()


def preserve(before,after):
    assert after['schema']==before['schema']=='0048_process_boundary'
    assert all(after['blobs'].get(k)==v for k,v in before['blobs'].items()),'Retained blob history changed'
    assert all(row in after['timeline'] for row in before['timeline']),'Retained timeline changed'


def qualify(bundle):
    scenarios=[]
    with fixture(bundle) as stack:
        compiler=stack.private('compiler')
        for revision in ('0019_custom_tools','0031_usage_details'):
            if scenarios:
                stack.stop();stack.restore_ownership();stack.home=stack.home/revision;stack.home.mkdir()
                for name in ('data','config','proofs','projects','overleaf'):(stack.home/name).mkdir()
            stack.seed(revision);stack.start()
            first=request(stack.base,'/api/health')
            if first.get('release'):
                assert first['release']['source']==bundle['source']
                assert first['capabilities']['release']['source']==bundle['source']
            assert first['capabilities']['database_boundary']['processes']=='separate'
            assert first['capabilities']['editor']['direct_lsp'] is False
            assert not first['capabilities']['execution']['available'],'Linux execution support must be explicitly qualified before enabling it'
            before=stack.private('snapshot',running=True)
            assert before['blobs']['b']
            assert request(stack.base,'/api/sessions/s')['code_steps'][0]['check_current'] is False
            # Inspect the production API itself; no source override is mounted.
            docker('exec',stack.names['api'],PYTHON,'-c',
                "from pathlib import Path;assert not any(Path(p).exists() for p in ('/app/prover','/app/config','/app/apps'));from app.main import app;import sys;assert not any(n=='lea' or n.startswith('lea.') for n in sys.modules)")
            docker('exec',stack.names['companion'],'node','--input-type=module','-e',
                "import {existsSync} from 'node:fs';for(const n of ['worker.mjs','workerService.mjs','databaseState.mjs'])if(existsSync('/app/apps/overleaf-extension/companion/'+n))throw Error(n)")
            with urlopen(stack.base,timeout=5) as response:assert b'<html' in response.read().lower()
            key=stack.private('producer',running=True)['credential'];headers={'Authorization':'Bearer '+key}
            body={'kind':'synthetic.echo','version':1,'idempotency_key':'durable','payload':{'text':'release receipt'}}
            accepted=request(stack.base,'/api/commands',body,headers)
            wait(lambda:request(stack.base,'/api/commands/'+accepted['id'],headers=headers)['state']=='succeeded')
            docker('restart',stack.names['api']);stack.set_base()
            wait(lambda:request(stack.base,'/api/health')['ready'])
            assert request(stack.base,'/api/health')['runtime_id']==first['runtime_id']
            assert request(stack.base,'/api/commands',body,headers)['id']==accepted['id']
            info=wait(lambda:(v if (v:=request(stack.base,'/api/sessions/s/editor')).get('environment_revision') else None))
            editor_owner=request(stack.base,'/api/sessions/s/editor/clients',{})
            editor_headers={'Authorization':'Bearer '+editor_owner['credential']}
            buffer=request(stack.base,'/api/editor/buffers',{'version':1,'path':info['path'],'content':info['content'],
                'base_revision':info['file_revision'],'environment_revision':info['environment_revision']},editor_headers)
            analysis=request(stack.base,'/api/editor/requests',{'version':1,'kind':'editor.analyze','buffer_id':buffer['id'],
                'environment_revision':buffer['environment_revision'],'sequence':1,'idempotency_key':'unsupported-runtime',
                'deadline':(datetime.now(timezone.utc)+timedelta(seconds=4.9)).isoformat()},editor_headers)
            refused=wait(lambda:(v if (v:=request(stack.base,'/api/editor/requests/'+analysis['id'],headers=editor_headers))['state'] not in {'queued','claimed'} else None),timeout=8)
            assert refused['error']=='confined_editor_executor_unavailable' and not refused['current'],refused
            assert refused['result'] is None
            # Distinct DB + identical ownership mount must not create a second writer.
            duplicate=stack.label+'-duplicate'
            docker('run','-d','--pull=never','--name',duplicate,'--network','none',*stack.env,*stack.mounts('second-data'),stack.image('worker'),'worker')
            stack.created.append(duplicate)
            wait(lambda:docker('inspect','--format','{{.State.Status}}',duplicate)=='exited',timeout=25)
            assert docker('inspect','--format','{{.State.ExitCode}}',duplicate)!='0'
            assert 'owns this physical workspace' in docker('logs',duplicate)
            docker('kill','--signal=KILL',stack.names['worker'])
            assert request(stack.base,'/api/sessions/s')['id']=='s'
            wait(lambda:not request(stack.base,'/api/health')['ready'],timeout=22)
            docker('start',stack.names['worker']);wait(lambda:request(stack.base,'/api/health')['ready'])
            assert request(stack.base,'/api/health')['generation']>first['generation']
            assert request(stack.base,'/api/commands/'+accepted['id'],headers=headers)['state']=='succeeded'
            after=stack.private('snapshot',running=True);preserve(before,after)
            assert after['proof_evidence_count']==before['proof_evidence_count']
            stack.drain()
            scenarios.append({'from_revision':revision,'production_role_entrypoints':True,'read_only_api_without_workspace':True,
                'legacy_storage_permissions_prepared':True,
                'stateless_companion':True,'retained_history':True,'independent_restarts':True,'durable_receipt_reused':True,
                'shared_workspace_duplicate_refused':True,'unsupported_execution_fail_closed':True,'worker_drain_preserves_api':True})
    return {'compiler':compiler,'scenarios':scenarios}


def rehearse(baseline,candidate):
    assert baseline['passed'] and candidate['passed'],'Both bundles must qualify before rollback'
    assert all(baseline['roles'][role]['id']!=candidate['roles'][role]['id'] for role in ('api','worker')),'Rollback requires distinct binaries, not retagged images'
    with fixture(baseline) as stack:
        stack.seed('0031_usage_details');stack.start();initial=stack.private('snapshot',running=True)
        stack.drain();stack.stop();stack.bundle=candidate;stack.start()
        health=request(stack.base,'/api/health');assert health.get('release',{}).get('version')==1,'Candidate build identity is missing'
        key=stack.private('producer',running=True)['credential'];headers={'Authorization':'Bearer '+key}
        body={'kind':'synthetic.echo','version':1,'idempotency_key':'candidate-receipt','payload':{'text':'retained after rollback'}}
        accepted=request(stack.base,'/api/commands',body,headers)
        wait(lambda:request(stack.base,'/api/commands/'+accepted['id'],headers=headers)['state']=='succeeded')
        uncertain=request(stack.base,'/api/commands',{'kind':'synthetic.uncertain','version':1,'idempotency_key':'uncertain',
            'deadline':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),'payload':{'text':'review required'}},headers)
        wait(lambda:request(stack.base,'/api/commands/'+uncertain['id'],headers=headers)['state']=='claimed')
        request(stack.base,'/api/skills',{'name':'retained-candidate-skill'})
        stack.drain();stack.stop()
        crash=stack.private('crash-publication');before=stack.private('snapshot');preserve(initial,before)
        stack.bundle=baseline;stack.start()
        assert 'release' not in request(stack.base,'/api/health'),'Older strict API binary was not restored'
        after=stack.private('verify-recovery',running=True);preserve(before,after)
        assert after['effects'][crash['effect_id']]=='published'
        assert request(stack.base,'/api/commands/'+uncertain['id'],headers=headers)['state']=='suspended'
        assert request(stack.base,'/api/commands',body,headers)['id']==accepted['id']
        assert request(stack.base,'/api/commands/'+accepted['id'],headers=headers)['result']['text']=='retained after rollback'
        assert any(s['name']=='retained-candidate-skill' for s in request(stack.base,'/api/skills')['skills'])
        stack.drain()
    return {'distinct_strict_binaries':True,'same_database_and_workspace_volumes':True,'schema_downgrade':False,
        'backup_restore':False,'volume_recreation':False,'candidate_history_and_receipt_preserved':True,
        'prepared_publication_recovered':True,'uncertain_claim_suspended':True,'old_claim_fenced':True,
        'baseline_build_identity_restored':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('mode',choices=['qualify','rollback'])
    parser.add_argument('--api-image');parser.add_argument('--worker-image');parser.add_argument('--companion-image')
    parser.add_argument('--require-native',action='store_true');parser.add_argument('--baseline',type=Path);parser.add_argument('--candidate',type=Path)
    parser.add_argument('--source');parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    if args.mode=='qualify':
        if not all((args.api_image,args.worker_image,args.companion_image)):parser.error('All production role images are required')
        bundle=images(args.api_image,args.worker_image,args.companion_image,args.require_native)
        bundle['source']=args.source
        if args.source:assert all(role['source']==args.source for role in bundle['roles'].values())
        result={**bundle,'source':args.source,'qualification':qualify(bundle)}
    else:
        baseline=json.loads(args.baseline.read_text());candidate=json.loads(args.candidate.read_text())
        result={'baseline':baseline['roles'],'candidate':candidate['roles'],'rehearsal':rehearse(baseline,candidate)}
    result.update(version=1,captured_at=datetime.now(timezone.utc).isoformat(),passed=True)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
