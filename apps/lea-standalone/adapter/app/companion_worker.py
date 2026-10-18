"""Private Node business worker ownership and execution."""
import json,os,re,secrets,subprocess
from pathlib import Path
from threading import Lock
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from . import db,commands
_application=Path(__file__).resolve().parents[2]
ROOT=_application.parents[1] if _application.parent.name=='apps' else _application
_lock=Lock();_child=None;_port=None;_bridge_key=None;_epoch=None
def _start(epoch):
    global _child,_port,_bridge_key,_epoch
    with _lock:
        if _child and _child.poll() is None and _epoch==epoch:return
        _close()
        from .config import load_config
        from .companion_import import import_directory
        config=load_config()
        legacy=Path(os.environ.get('LEA_COMPANION_IMPORT_DIR',ROOT/'apps/overleaf-extension/.overleaf-lean-stub'))
        import_directory(legacy)
        _bridge_key=secrets.token_urlsafe(32);_epoch=epoch
        with db.write() as conn:
            conn.execute("update worker_process set bridge_hash=? where id=1 and lifecycle_state='ready'",(commands._hash(_bridge_key),))
        base='http://127.0.0.1:'+os.environ['LEA_ITEST_PORT'] if os.environ.get('LEA_ITEST_PORT') else os.environ.get('LEA_API_BASE_URL') or 'http://127.0.0.1:'+os.environ.get('LEA_API_PORT','8001')
        inherited={k:v for k,v in os.environ.items() if not re.search('API_KEY|AUTH_TOKEN|GITHUB_TOKEN|PASSWORD|SECRET',k) and k not in {'LEA_REPO_PATH','LEA_MODEL','LEA_MAX_TURNS','LEA_MAX_SPEND_USD'}}
        env={**inherited,'LEA_WORKER_DB':str(db.DB_PATH),'LEA_WORKER_EPOCH':epoch,'LEA_WORKER_BRIDGE_KEY':_bridge_key,
             'LEA_API_BASE_URL':base,'LEA_ROOT':str(config.lea_root or ROOT/'apps/lea-standalone/prover')}
        from lea import process_ownership
        lease=process_ownership.prepare()
        try:
            _child=subprocess.Popen([os.environ.get('LEA_NODE','node'),str(ROOT/'apps/overleaf-extension/companion/worker.mjs')],
                env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=None,text=True,start_new_session=True,
                pass_fds=(lease.fd,) if lease else ())
            process_ownership.register(_child.pid,lease)
        finally:process_ownership.release_parent(lease,spawned=_child is not None)
        import select
        if not select.select([_child.stdout],[],[],30)[0]:_close();raise RuntimeError('Business worker startup timed out')
        line=_child.stdout.readline()
        try:_port=json.loads(line)['port']
        except (ValueError,KeyError):_close();raise RuntimeError('Business worker did not start')


def execute(token):
    _start(token['worker_epoch'])
    with db.connect() as conn:
        if not commands._owned(conn,token):return
        row=conn.execute('select * from companion_requests where command_id=?',(token['id'],)).fetchone()
    request=Request(f'http://127.0.0.1:{_port}'+row['path'],data=row['payload'].encode(),method=row['method'],
        headers={'Content-Type':'application/json','X-Lea-Worker':_bridge_key,'X-Lea-Command-ID':token['id']})
    try:
        with urlopen(request,timeout=900) as response:
            raw=response.read(64*1024*1024+1);status=response.status
    except HTTPError as exc:raw=exc.read(1024*1024);status=exc.code
    if len(raw)>64*1024*1024:raise ValueError('Companion result exceeds its 64 MiB bound')
    try:body=json.loads(raw)
    except ValueError:body={'error':'invalid_worker_response'};status=502
    result={'statusCode':status,'body':body}
    with db.write() as conn:
        owned=commands._owned(conn,token)
        if not owned:return
        conn.execute('update companion_requests set result_json=? where command_id=?',(json.dumps(result),token['id']))
        # A domain rejection is an observed operation result, not an execution
        # transport failure. Preserve its original HTTP status and detail.
        commands._settle(conn,owned,'succeeded',{'companion_request':token['id']})


def _close():
    global _child,_port
    if _child:
        from lea import process_ownership
        import signal
        if _child.poll() is None:
            os.killpg(_child.pid,signal.SIGTERM)
            try:_child.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(_child.pid,signal.SIGKILL);_child.wait(timeout=5)
        process_ownership.unregister(_child.pid)
        if _child.stdout:_child.stdout.close()
    _child=None;_port=None


def close():
    with _lock:_close()

