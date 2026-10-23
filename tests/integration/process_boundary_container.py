"""Local-only Stage 8 container probe; no image pulls, model calls or publication.

Build the API qualification image first. The worker uses the cached runtime with
this checkout mounted read-only; API receives only the current copied code/UI
and its DB directory. Every container/network/directory is owned by this probe.
"""
import argparse,json,os,pathlib,subprocess,tempfile,time,uuid
from urllib.request import Request,urlopen
ROOT=pathlib.Path(__file__).resolve().parents[2]


def docker(*args):
    try:return subprocess.check_output(['docker',*map(str,args)],text=True,stderr=subprocess.STDOUT).strip()
    except subprocess.CalledProcessError as exc:raise RuntimeError('Docker probe failed:\n'+exc.output) from exc


def wait(fn,timeout=35):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            result=fn()
            if result:return result
        except OSError:pass
        time.sleep(.15)
    raise RuntimeError('Container boundary condition timed out')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--api-image',default='lea-stage8-api-qualification:local')
    parser.add_argument('--worker-image',default='lea-stage6-qualification:local');parser.add_argument('--report')
    args=parser.parse_args();label='lea-boundary-'+uuid.uuid4().hex[:10]
    api=label+'-api';worker=label+'-worker';created=[]
    with tempfile.TemporaryDirectory(prefix='lea-container-boundary-') as folder:
        folder=pathlib.Path(folder);data=folder/'db';work=folder/'work';data.mkdir();work.mkdir()
        (work/'config.toml').write_text('model="openai/gpt-test"\nlea_root="/work/prover"\n')
        try:
            # Use the existing default bridge: qualification must not consume a
            # new address pool or modify unrelated development networks.
            docker('run','--rm','--pull=never','-v',f'{data}:/state',args.api_image,'bootstrap.py')
            docker('run','-d','--pull=never','--name',api,'--network','bridge','--read-only',
                '--cap-drop=ALL','--security-opt=no-new-privileges:true','--tmpfs','/tmp',
                '-e','LEA_ALLOWED_HOSTS=api','-v',f'{data}:/state','-p','127.0.0.1::8001',args.api_image)
            created.append(api)
            address=docker('port',api,'8001/tcp');base='http://'+address
            def http(path,body=None,headers=None):
                req=Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type':'application/json',**(headers or {})})
                with urlopen(req,timeout=4) as response:return json.load(response)
            assert not wait(lambda:http('/api/health'))['ready']
            docker('exec',api,'/app/adapter/.venv/bin/python','-c',
                "from pathlib import Path;assert not any(Path(p).exists() for p in ('/app/prover','/app/config','/app/apps','/work'));from app.main import app;import sys;assert not any(n=='lea' or n.startswith('lea.') for n in sys.modules)")
            docker('run','-d','--pull=never','--name',worker,'--network','bridge','--link',api+':api',
                '-v',f'{data}:/state','-v',f'{work}:/work','-v',f'{ROOT}:/src:ro',
                '-e','PYTHONPATH=/src/apps/lea-standalone/adapter:/src/apps/lea-standalone/prover',
                '-e','LEA_SHARED_DATA_DIR=/state','-e','LEA_CONFIG_FILE=/work/config.toml',
                '-e','LEA_COMPANION_IMPORT_DIR=/work/legacy','-e','LEA_MODEL_CATALOG_OFFLINE=true',
                '-e','LITELLM_LOCAL_MODEL_COST_MAP=True','-e','LEA_API_BASE_URL=http://api:8001',
                '--entrypoint','/app/adapter/.venv/bin/python',args.worker_image,
                '/src/apps/lea-standalone/adapter/run_worker.py')
            created.append(worker)
            wait(lambda:http('/api/health')['ready'])
            first=http('/api/health')
            credential=docker('exec','-w','/src/apps/lea-standalone/adapter',worker,'/app/adapter/.venv/bin/python','-c',
                "from app import commands;print(commands.register_producer('container-boundary'))").splitlines()[-1]
            headers={'Authorization':'Bearer '+credential}
            body={'kind':'synthetic.echo','version':1,'idempotency_key':'once','payload':{'text':'SQL only'}}
            accepted=http('/api/commands',body,headers)
            settled=wait(lambda:(v if (v:=http('/api/commands/'+accepted['id'],headers=headers))['state']=='succeeded' else None))
            assert settled['result']['text']=='SQL only'
            docker('restart',api)
            base='http://'+docker('port',api,'8001/tcp')
            wait(lambda:http('/api/health')['ready'])
            assert http('/api/health')['runtime_id']==first['runtime_id']
            assert http('/api/commands',body,headers)['id']==accepted['id']
            docker('kill','--signal=KILL',worker)
            assert http('/api/sessions')['sessions']==[]
            wait(lambda:not http('/api/health')['ready'],timeout=22)
            docker('start',worker)
            wait(lambda:http('/api/health')['ready'])
            after=http('/api/health')
            assert after['generation']>first['generation'] and after['runtime_id']!=first['runtime_id']
            assert http('/api/commands/'+accepted['id'],headers=headers)['state']=='succeeded'
            result={'schema':1,'platform':docker('exec',api,'uname','-m'),'api_workspace_absent':True,
                'read_only_api':True,'api_restart_preserves_worker':True,'worker_crash_preserves_api_reads':True,
                'durable_receipt_reused':True,'worker_generation_advanced':True,'api_image':args.api_image,
                'worker_image':args.worker_image,'full_release_image_build':False,'native_linux_lean_qualified':False}
            if args.report:pathlib.Path(args.report).write_text(json.dumps(result,indent=2)+'\n')
            print(json.dumps(result))
        except BaseException:
            for name in created:
                try:print(docker('logs','--tail','40',name))
                except RuntimeError:pass
            raise
        finally:
            for name in reversed(created):
                try:docker('rm','-f',name)
                except RuntimeError:pass

if __name__=='__main__':main()
