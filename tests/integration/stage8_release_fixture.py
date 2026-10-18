"""Private, keyless release fixtures, run only in disposable worker containers.

Never mounted in the public API. A crash seam stops file publication after its
real SQL intent commits, so rollback must run the production journal recovery.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0,'/app/adapter')
from app import db


def snapshot():
    with db.read_snapshot() as conn:
        return {'schema':conn.execute('select version_num from alembic_version').fetchone()[0],
            'proof_evidence_count':conn.execute('select count(*) from proof_check_evidence').fetchone()[0],
            'blobs':{r['id']:hashlib.sha256(r['content'].encode()).hexdigest() for r in conn.execute('select id,content from artifact_blobs')},
            'timeline':[[r[0],r[1],r[2]] for r in conn.execute('select id,path,after_blob_id from timeline order by id')],
            'effects':{r[0]:r[1] for r in conn.execute('select id,state from workspace_effects')},
            'commands':{r[0]:r[1] for r in conn.execute('select id,state from commands')}}


def compiler():
    """Test shipped trusted compiler/kernel binaries, not an execution fallback."""
    root=Path('/app/prover/workspace')
    def run(args,cwd=root):
        result=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=180)
        assert result.returncode==0,result.stdout+result.stderr
        return result.stdout.strip()
    lean=run(['lake','env','lean','--version'])
    run(['lake','build','--no-build','Mathlib'])
    with tempfile.TemporaryDirectory(prefix='ReleaseProbe',dir=root) as folder:
        folder=Path(folder);source=folder/'Probe.lean';target=folder/'Target.lean'
        source.write_text('import Mathlib.Data.Nat.Prime.Basic\ntheorem release_probe (n : Nat) : n = n := by rfl\n')
        target.write_text('import Mathlib.Data.Nat.Prime.Basic\ntheorem release_probe (n : Nat) : n = n := by sorry\n')
        for path in (source,target):run(['lake','env','lean','-o',str(path.with_suffix('.olean')),str(path)])
        binary='/app/prover/third_party/SafeVerify/.lake/build/bin/safe_verify'
        run(['lake','env',binary,'--disallow-partial',str(target.with_suffix('.olean')),str(source.with_suffix('.olean'))])
        bad=subprocess.run(['lake','env',binary,'--disallow-partial',str(target.with_suffix('.olean')),str(target.with_suffix('.olean'))],cwd=root,capture_output=True,text=True,timeout=180)
        assert bad.returncode!=0,'SafeVerify accepted a sorry proof'
    from lea import execution
    return {'lean':lean,'complete_mathlib':True,'real_compilation':True,
        'safeverify_accepts_proof':True,'safeverify_rejects_sorry':True,
        'confined_execution':execution.capability(),
        'note':'Trusted packaging probes do not enable application execution on unsupported platforms.'}


def crash_publication():
    from app import runtime,commands,workspace_commands,workspace_files
    from app.routes import sessions
    from app.config import load_config
    config=load_config();runtime.acquire_data_lock();runtime.acquire_workspace_lock(config.lea_root/'workspace')
    from lea import process_ownership
    os.environ['LEA_OWNED_PROCESS_DIR']=str(runtime.owned_process_dir());process_ownership.reap_previous()
    with db.write() as conn:conn.execute("delete from command_workers where id='lea-command-host'")
    receipt=workspace_commands.admit('session.save',{'session_id':'s','request':{
        'path':'Proof.lean','content':Path('/app/prover/workspace/proofs/Lea/Readiness/Proof.lean').read_text()+'-- retained publication across binary rollback\n'}},idempotency_key='rollback-crash-publication')
    epoch=commands.start_worker('lea-command-host')
    token=commands.claim('lea-command-host',epoch,'compute',kinds={'workspace.execute'})
    assert token and token['id']==receipt['command']['id']
    class SimulatedCrash(BaseException):pass
    def interrupted(*_,**__):raise SimulatedCrash()
    workspace_files.materialize=interrupted
    try:workspace_commands.execute(token)
    except SimulatedCrash:pass
    else:raise AssertionError('Crash seam was not reached')
    with db.write() as conn:
        effect=dict(conn.execute('select * from workspace_effects where command_id=?',(token['id'],)).fetchone())
        assert effect['state']=='prepared'
        conn.execute("update commands set lease_until='2000-01-01T00:00:00+00:00' where state='claimed'")
    (db.DB_PATH.parent/'rollback-old-claim.json').write_text(json.dumps(token))
    return {'command_id':token['id'],'effect_id':effect['id'],'state':effect['state']}


def verify_recovery():
    from app import commands
    old=json.loads((db.DB_PATH.parent/'rollback-old-claim.json').read_text())
    assert not commands.complete(old,{'stale':True}),'Prior candidate claim was not fenced'
    value=snapshot()
    assert value['commands'][old['id']]=='succeeded',value
    assert '-- retained publication across binary rollback' in Path('/app/prover/workspace/proofs/Lea/Readiness/Proof.lean').read_text()
    with db.connect() as conn:
        assert not conn.execute("select 1 from workspace_effects where state in ('prepared','installed')").fetchone()
    return value


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='snapshot':value=snapshot()
    elif mode=='compiler':value=compiler()
    elif mode=='producer':
        from app import commands
        value={'credential':commands.register_producer('release-probe')}
    elif mode=='crash-publication':value=crash_publication()
    elif mode=='verify-recovery':value=verify_recovery()
    else:raise SystemExit('Unknown private release fixture')
    print(json.dumps(value),flush=True)
