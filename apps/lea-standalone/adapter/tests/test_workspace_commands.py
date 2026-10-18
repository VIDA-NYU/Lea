"""Real SQL admission and worker execution; no models or compiler substitutes hidden in intake."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import commands, db, store, workspace_commands as queue, workspace_files, workspace_effects
from app.config import LeaConfig
from app.gitstore import GitStore
from app.routes import sessions, projects, workspace, formalizations, project_library


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'scratch.sqlite3')
    db.init_db()
    config = LeaConfig(model='test', lea_root=tmp_path, max_turns=3)
    for module in (sessions, projects):
        monkeypatch.setattr(module, 'load_config', lambda: config)
    import app.config
    monkeypatch.setattr(app.config, 'load_config', lambda: config)
    session = store.create_session('Workspace edits')
    gs = GitStore(tmp_path / 'workspace/proofs')
    repo = gs.init_session(session['id'])
    path = repo / 'P.lean'
    path.write_text('theorem p : True := by trivial\n')
    store.add_code_step(session['id'], None, 'P.lean', content=path.read_text(), author='agent')
    epoch = commands.start_worker('workspace-test')
    return session, repo, epoch


def submit(env, content='theorem p : True := by decide\n', key='save', base=None):
    session, _, _ = env
    return queue.admit('session.save', {'session_id': session['id'], 'request': {'path':'P.lean','content':content,'note':'Edit explanation'}},
        idempotency_key=key, expected_revision=base or queue.revision_for('s:'+session['id'],'P.lean'), draft_id='draft-'+key, strict=True)


def execute(env):
    token = commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
    assert token
    assert queue.execute(token)
    return queue.get(token['id'])['command']


def test_admission_is_sql_only_and_retains_immutable_draft(env, monkeypatch):
    monkeypatch.setattr(sessions.fs_service,'safe_abs',lambda *a:pytest.fail('request reached workspace'))
    receipt = submit(env)
    assert receipt['command']['state']=='queued'
    assert env[1].joinpath('P.lean').read_text().endswith('trivial\n')
    with db.connect() as conn:
        revision=conn.execute('select * from workspace_file_revisions').fetchone()
        body=conn.execute('select payload_json from commands').fetchone()[0]
        assert 'by decide' not in body
        assert store.blob_content(revision['blob_id']).endswith('decide\n')


def test_new_api_requires_base_and_valid_action_arguments(env):
    sid=env[0]['id']
    with pytest.raises(HTTPException) as error:
        queue.admit('session.save',{'session_id':sid,'request':{'path':'P.lean','content':'draft'}},strict=True)
    assert error.value.status_code==422
    with pytest.raises(HTTPException):
        queue.admit('session.save',{'session_id':sid,'request':{'content':'missing path'}},strict=True)
    with db.connect() as conn:
        assert conn.execute('select count(*) from commands').fetchone()[0]==0


def test_concurrent_duplicate_admission_has_one_draft_and_capacity_reservation(env):
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts=list(executor.map(lambda _:submit(env),range(4)))
    assert len({r['command']['id'] for r in receipts})==1
    with db.connect() as conn:
        assert conn.execute('select count(*) from workspace_file_revisions').fetchone()[0]==1
        assert conn.execute("select count from command_capacity where key='compute:global'").fetchone()[0]==1
    with pytest.raises(commands.QueueError):
        submit(env,content='different',key='save')


def test_save_publishes_sql_history_note_and_exact_file(env):
    submit(env)
    result=execute(env)
    assert result['state']=='succeeded',result
    assert result['result']['code_step']['author']=='user'
    assert result['result']['note']['content']=='Edit explanation'
    assert result['result']['draft_id']=='draft-save'
    assert result['result']['code_step']['code']==env[1].joinpath('P.lean').read_text()
    with db.connect() as conn:
        assert conn.execute('select state from workspace_effects').fetchone()[0]=='published'


def test_two_saves_against_one_base_preserve_later_draft_as_conflict(env):
    base=queue.revision_for('s:'+env[0]['id'],'P.lean')
    submit(env,key='first',base=base)
    submit(env,content='newer unsaved draft',key='second',base=base)
    assert execute(env)['state']=='succeeded'
    second=execute(env)
    assert second['state']=='failed' and second['error_status']==409
    assert second['result']['detail']['code']=='revision_conflict'
    with db.connect() as conn:
        rows=conn.execute('select blob_id from workspace_file_revisions').fetchall()
        assert 'newer unsaved draft' in [store.blob_content(r[0]) for r in rows]


def test_external_change_is_preserved_with_conflict_evidence(env):
    submit(env)
    env[1].joinpath('P.lean').write_text('external editor bytes')
    result=execute(env)
    assert result['state']=='failed' and result['error_status']==409
    assert env[1].joinpath('P.lean').read_text()=='external editor bytes'
    with db.connect() as conn:
        row=conn.execute('select * from workspace_effects').fetchone()
        assert row['state']=='conflict'
        assert json.loads(row['manifest_json'])['external_blob']
    assert store.current_proof_step(env[0]['id'],'P.lean')['code'].endswith('trivial\n')


class KilledWorker(BaseException):
    pass


@pytest.mark.parametrize('boundary',['intent','rename','install','published'])
def test_restart_reconciles_each_file_publication_boundary(env,monkeypatch,boundary):
    submitted=submit(env)
    token=commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
    if boundary=='intent':
        target=workspace_files.materialize
        monkeypatch.setattr(workspace_files,'materialize',lambda *a,**k:(_ for _ in ()).throw(KilledWorker()))
    elif boundary=='rename':
        target=workspace_files._install
        monkeypatch.setattr(workspace_files,'_install',lambda *a,**k:(_ for _ in ()).throw(KilledWorker()))
    elif boundary=='install':
        target=workspace_files._install
        def install_then_die(*args):
            target(*args)
            raise KilledWorker()
        monkeypatch.setattr(workspace_files,'_install',install_then_die)
    else:
        target=workspace_files.provenance
        def published_then_die(*args,**kwargs):
            target(*args,**kwargs)
            raise KilledWorker()
        monkeypatch.setattr(workspace_files,'provenance',published_then_die)
    with pytest.raises(KilledWorker):queue.execute(token)
    monkeypatch.setattr(workspace_files,'materialize' if boundary=='intent' else 'provenance' if boundary=='published' else '_install',target)
    workspace_files.recover()
    queue.recover_at_startup()
    result=queue.get(submitted['command']['id'])['command']
    assert result['state']=='succeeded',result
    assert result['result']['recovered']
    assert env[1].joinpath('P.lean').read_text().endswith('decide\n')
    with db.connect() as conn:
        assert conn.execute("select count(*) from timeline where kind='code'").fetchone()[0]==2
        assert conn.execute("select count(*) from timeline where kind='edit_note'").fetchone()[0]==1


def test_recovery_never_overwrites_external_edits(env,monkeypatch):
    receipt=submit(env)
    token=commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
    with monkeypatch.context() as change:
        change.setattr(workspace_files,'materialize',lambda *a,**k:(_ for _ in ()).throw(KilledWorker()))
        with pytest.raises(KilledWorker):queue.execute(token)
    env[1].joinpath('P.lean').write_text('external after crash')
    workspace_files.recover();queue.recover_at_startup()
    assert queue.get(receipt['command']['id'])['command']['state']=='suspended'
    assert env[1].joinpath('P.lean').read_text()=='external after crash'


def test_receipt_lookup_survives_lost_response(env):
    receipt=submit(env,key='lost-receipt')
    assert queue.get(idempotency_key='lost-receipt')['command']['id']==receipt['command']['id']
    assert submit(env,key='lost-receipt')['command']['id']==receipt['command']['id']


def test_completed_duplicate_returns_original_domain_result(env):
    base=queue.revision_for('s:'+env[0]['id'],'P.lean')
    submit(env,key='done',base=base);original=execute(env)
    duplicate=submit(env,key='done',base=base)['command']
    assert duplicate['state']=='succeeded'
    assert duplicate['result']['code_step']['code']==original['result']['code_step']['code']


def test_http_translator_returns_receipt_without_executing(env,monkeypatch):
    api=FastAPI();api.include_router(sessions.router);api.include_router(workspace.router)
    monkeypatch.setattr(sessions.projects,'resolve_git',lambda *a:pytest.fail('HTTP crossed workspace boundary'))
    with TestClient(api) as client:
        response=client.post('/api/sessions/'+env[0]['id']+'/file',json={'path':'P.lean','content':'draft'})
        assert response.status_code==202,response.text
        receipt=response.json()['command']
        assert client.get('/api/workspace/commands/'+receipt['id']).json()['command']['state']=='queued'
        value=client.get('/api/workspace/file',params={'session_id':env[0]['id'],'path':'P.lean'}).json()
        assert value['content'].endswith('trivial\n')


def test_multipart_receipt_replay_uses_original_header_key(env):
    p=store.create_project('upload-idempotency')
    api=FastAPI();api.include_router(projects.router);api.include_router(workspace.router)
    with TestClient(api) as client:
        responses=[client.post('/api/projects/'+p['id']+'/files',files={'file':('notes.txt',b'attachment bytes','text/plain')},
            headers={'Idempotency-Key':'original-upload'}) for _ in range(2)]
        assert all(r.status_code==202 for r in responses)
        assert responses[0].json()['command']['id']==responses[1].json()['command']['id']
        assert client.get('/api/workspace/receipts',params={'idempotency_key':'original-upload'}).status_code==200
    with db.connect() as conn:assert conn.execute('select count(*) from commands').fetchone()[0]==1


def test_claim_loss_fences_publication_before_intent(env):
    receipt=submit(env)
    token=commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
    commands.revoke(token['producer_id'])
    assert not queue.execute(token)
    assert queue.get(receipt['command']['id'])['command']['state']=='suspended'
    assert env[1].joinpath('P.lean').read_text().endswith('trivial\n')


def test_nonproof_content_is_sql_authority_when_git_fails(env,monkeypatch,tmp_path):
    p=store.create_project('Docs')
    repo=tmp_path/'workspace/proofs'/Path(p['namespace'].replace('.','/'))
    repo.mkdir(parents=True,exist_ok=True)
    queue.admit('project.save',{'project_id':p['id'],'request':{'path':'notes.txt','content':'retained notes'}},expected_revision='missing',strict=True)
    monkeypatch.setattr(GitStore,'commit_all',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('Git unavailable')))
    result=execute(env)
    assert result['state']=='failed',result
    with db.connect() as conn:
        row=conn.execute('select * from workspace_files where project_id=?',(p['id'],)).fetchone()
        assert row['materialization']=='provenance_pending'
        assert store.blob_content(row['blob_id'])=='retained notes'
    assert repo.joinpath('notes.txt').read_text()=='retained notes'


def test_strict_missing_base_cannot_overwrite_an_unobserved_existing_file(env,tmp_path):
    from app.projects import project_repo_dir
    p=store.create_project('unobserved')
    repo=project_repo_dir(p,tmp_path/'workspace/proofs');repo.mkdir(parents=True)
    (repo/'notes.txt').write_text('unobserved external bytes')
    queue.admit('project.save',{'project_id':p['id'],'request':{'path':'notes.txt','content':'new draft'}},expected_revision='missing',strict=True)
    result=execute(env)
    assert result['state']=='failed' and result['error_status']==409
    assert (repo/'notes.txt').read_text()=='unobserved external bytes'


def test_retirement_and_restore_update_current_sql_binding(env,tmp_path):
    from app.projects import project_repo_dir
    p=store.create_project('retirement');session=store.create_session('proof',project_id=p['id'])
    repo=project_repo_dir(p,tmp_path/'workspace/proofs');GitStore(repo.parent).init_session(repo.name)
    content='theorem p : True := by trivial\n';(repo/'P.lean').write_text(content)
    store.add_code_step(session['id'],None,'P.lean',content=content,check_status='ok',author='agent')
    queue.admit('project.retire',{'slug':p['slug'],'request':{'path':'P.lean'}})
    retired=execute(env)
    assert retired['state']=='succeeded',retired
    assert workspace.file('P.lean',project_id=p['id'])['content'] is None
    assert queue.revision_for('p:'+p['id'],'P.lean').startswith('deleted:')
    queue.admit('project.restore',{'slug':p['slug'],'request':{'path':'P.lean','retire_commit':retired['result']['retire_commit']}})
    restored=execute(env)
    assert restored['state']=='succeeded',restored
    assert workspace.file('P.lean',project_id=p['id'])['content']==content
    assert (repo/'P.lean').read_text()==content


def test_default_name_updates_docs_without_holding_sql_writer_during_git(env,monkeypatch,tmp_path):
    from app import projects as service
    p=service.provision_project('Generated',tmp_path/'workspace/proofs')
    with db.write() as conn:conn.execute("update projects set name_source='generated' where id=?",(p['id'],))
    commit=GitStore.commit_all
    def record(*args,**kwargs):
        with db.write() as conn:
            conn.execute("insert or ignore into command_capacity values('git-does-not-own-sql',0)")
        return commit(*args,**kwargs)
    monkeypatch.setattr(GitStore,'commit_all',record)
    queue.admit('project.default_name',{'slug':p['slug'],'request':{'overleaf_project_title':'Document title'}})
    result=execute(env)
    assert result['state']=='succeeded',result
    assert result['result']['identity']['projectName']=='Document title'
    assert 'Document title' in workspace.file('.lea/instructions.md',project_id=p['id'])['content']


def test_queue_capacity_failure_rolls_back_blob_draft_and_request(env,monkeypatch):
    from app import migration_limits
    monkeypatch.setattr(migration_limits,'COMPUTE_QUEUED_GLOBAL',0)
    with db.connect() as conn:before=conn.execute('select count(*) from artifact_blobs').fetchone()[0]
    with pytest.raises(commands.QueueError) as error:submit(env)
    assert error.value.status==429
    with db.connect() as conn:
        assert conn.execute('select count(*) from artifact_blobs').fetchone()[0]==before
        assert conn.execute('select count(*) from workspace_requests').fetchone()[0]==0
        assert conn.execute('select count(*) from workspace_file_revisions').fetchone()[0]==0


def test_cached_readiness_observes_new_commits_without_holding_snapshot(env):
    with commands.readiness_reader() as ready:
        assert not ready('compute',kinds=queue.KINDS)
        submit(env)
        assert ready('compute',kinds=queue.KINDS)
        token=commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
        assert token and not ready('compute',kinds=queue.KINDS)


def test_compaction_retains_actual_usage_but_fences_late_transcript(env,monkeypatch):
    from types import SimpleNamespace
    from lea.providers import Usage
    session=env[0]
    run=store.create_run(session['id'],'test',None,3)
    original=[{'role':'user','content':'Existing conversation'}]
    store.update_run(run['id'],'needs_review')
    store.set_run_transcript(run['id'],original)
    queue.admit('session.compact',{'session_id':session['id']})
    token=commands.claim('workspace-test',env[2],'compute',kinds=queue.KINDS)
    def condensed(*a,**k):
        commands.revoke(token['producer_id'])
        return SimpleNamespace(changed=True,pruned=1,summarized=True,before_tokens=50,after_tokens=10,
            messages=[{'role':'user','content':'Condensed'}],usage=Usage(10,2),cost=.001)
    monkeypatch.setattr(sessions.condenser,'condense',condensed)
    assert not queue.execute(token)
    with db.connect() as conn:
        row=conn.execute('select transcript,cost_usd from runs where id=?',(run['id'],)).fetchone()
        assert json.loads(row['transcript'])==original
        assert row['cost_usd']==pytest.approx(.001)
        assert conn.execute("select completed_at from run_invocations where name='manual_compaction'").fetchone()[0]


def checked(path, **kwargs):
    import hashlib
    from lea.interface import CheckResult
    from lea.check_evidence import environment_hash,lake_root,_configuration,source_identity
    raw=Path(path).read_bytes();root=lake_root(path)
    environment={'configuration':_configuration(root) if root else {},'files':{}}
    bad=b'bad_tactic' in raw
    return CheckResult(path,'error' if bad else 'ok','bad tactic' if bad else None,
        execution={'check_id':os.urandom(8).hex(),'evidence_version':1,'content_sha256':hashlib.sha256(raw).hexdigest(),
            'source_path':path,'source_identity':source_identity(path),'environment':environment,'environment_sha256':environment_hash(environment)},content=raw.decode())


def test_failed_lean_verdict_is_successful_command_execution(env,monkeypatch):
    monkeypatch.setattr(sessions,'interface_check',checked)
    monkeypatch.setattr(sessions,'lean_check_runtime',lambda _: {'state':'healthy'})
    submit(env,content='theorem p : True := by bad_tactic\n');execute(env)
    queue.admit('session.check',{'session_id':env[0]['id'],'request':{'path':'P.lean'}})
    result=execute(env)
    assert result['state']=='succeeded',result
    assert result['result']['status']=='error'
    assert store.current_proof_step(env[0]['id'],'P.lean')['check_status']=='error'


def test_unchanged_save_preserves_checked_head_and_history(env):
    original=store.current_proof_step(env[0]['id'],'P.lean')
    submit(env,content=original['code'])
    result=execute(env)
    assert result['result']['unchanged']
    assert store.current_proof_step(env[0]['id'],'P.lean')['id']==original['id']


@pytest.mark.parametrize('failed_module',[None,'B.lean'])
def test_manual_edit_cascade_is_worker_owned_and_transitive(env,monkeypatch,tmp_path,failed_module):
    from app.projects import project_repo_dir
    checks=[]
    def check(path,**kwargs):checks.append(Path(path).name);return checked(path,**kwargs)
    monkeypatch.setattr(sessions,'interface_check',check)
    monkeypatch.setattr(sessions,'lean_check_runtime',lambda _: {'state':'healthy'})
    builds=[]
    def rebuild(path):
        builds.append(Path(path).name)
        result=checked(path)
        if Path(path).name==failed_module:
            from lea.interface import CheckResult
            return CheckResult(path,'error','Source build failed')
        return result
    monkeypatch.setattr(sessions,'interface_rebuild',rebuild)
    p=store.create_project('Cascade')
    repo=project_repo_dir(p,tmp_path/'workspace/proofs');repo.mkdir(parents=True)
    items={}
    for name,imports in [('A',[]),('B',['A']),('C',['B'])]:
        s=store.create_session(name,project_id=p['id']);items[name]=s
        content=''.join('import '+p['namespace']+'.'+n+'\n' for n in imports)+f'theorem {name.lower()} : True := by trivial\n'
        (repo/(name+'.lean')).write_text(content)
        store.add_code_step(s['id'],None,name+'.lean',content=content,author='agent')
    (repo/'Unowned.lean').write_text('import '+p['namespace']+'.A\ntheorem unowned : True := by trivial\n')
    queue.admit('session.edit',{'session_id':items['A']['id'],'request':{'path':'A.lean','content':'theorem a : True := by decide\n'}},
        expected_revision=queue.revision_for('p:'+p['id'],'A.lean'),strict=True)
    result=execute(env)
    assert result['state']=='succeeded',result
    assert builds==['A.lean','B.lean','C.lean']
    recorded=[d for d in result['result']['dependents'] if d['session_id']]
    assert [d['path'] for d in recorded]==['B.lean','C.lean']
    assert next(d for d in result['result']['dependents'] if d['path']=='Unowned.lean')['status']=='unknown'
    if failed_module:
        assert result['result']['dependents'][0]['status']=='error'
        assert failed_module not in checks
        assert store.current_proof_step(items['B']['id'],'B.lean')['check_status']=='error'
    else:
        assert all(d['status']=='ok' for d in recorded)


def test_cleanup_retains_repository_before_sql_deletion(env,tmp_path):
    from app.projects import project_repo_dir
    p=store.create_project('delete-safely')
    repo=project_repo_dir(p,tmp_path/'workspace/proofs');repo.mkdir(parents=True)
    (repo/'retained.txt').write_text('recoverable before-image')
    receipt=queue.admit('project.delete',{'project_id':p['id']})
    result=execute(env)
    assert result['state']=='succeeded',result
    assert store.get_project(p['id']) is None and not repo.exists()
    tombstone=tmp_path/'workspace/proofs/.lea-trash'/receipt['command']['id']
    assert tombstone.joinpath('retained.txt').read_text()=='recoverable before-image'


def test_workspace_effect_blocks_new_work_until_reconciliation(env):
    p=store.create_project('Interrupted')
    with db.write() as conn:
        conn.execute('insert into workspace_effects values(?,?,?,?,?,?,?,?,?)',('effect','old',p['id'],None,'operation','conflict','{}',db.utc_now(),db.utc_now()))
    with pytest.raises(HTTPException) as error:queue.admit('project.delete',{'project_id':p['id']})
    assert error.value.detail['code']=='workspace_reconciliation_required'


@pytest.mark.parametrize('boundary',['before_intent','intent','rename','install','publication'])
def test_sigkill_recovery_retains_draft_and_reconciles_content(env,tmp_path,boundary):
    """A different process dies without Python cleanup at each publication seam."""
    receipt=submit(env)
    script=r'''
import os,signal,sys
from pathlib import Path
from app import db,commands,workspace_commands as q,workspace_files as files
from app.routes import sessions
from app.config import LeaConfig
db.DB_PATH=Path(sys.argv[1]);root=Path(sys.argv[2]);boundary=sys.argv[3]
sessions.load_config=lambda:LeaConfig(model='test',lea_root=root,max_turns=3)
import app.config
app.config.load_config=sessions.load_config
def die(*a,**k):os.kill(os.getpid(),signal.SIGKILL)
if boundary=='before_intent':files.save=die
elif boundary=='intent':files.materialize=die
elif boundary=='rename':files._install=die
elif boundary=='install':
    original=files._install
    def install(*a,**k):original(*a,**k);die()
    files._install=install
else:
    original=files.provenance
    def publish(*a,**k):original(*a,**k);die()
    files.provenance=publish
epoch=commands.start_worker('killed-worker')
token=commands.claim('killed-worker',epoch,'compute',kinds=q.KINDS)
q.execute(token)
'''
    child=subprocess.run([sys.executable,'-c',script,str(db.DB_PATH),str(tmp_path),boundary],capture_output=True,text=True,timeout=30)
    assert child.returncode==-signal.SIGKILL,child.stdout+child.stderr
    workspace_files.recover();workspace_effects.recover();queue.recover_at_startup()
    result=queue.get(receipt['command']['id'])['command']
    if boundary=='before_intent':
        assert result['state']=='queued'
        assert env[1].joinpath('P.lean').read_text().endswith('trivial\n')
    else:
        assert result['state']=='succeeded',result
        assert env[1].joinpath('P.lean').read_text().endswith('decide\n')
    with db.connect() as conn:
        assert conn.execute('select count(*) from workspace_file_revisions').fetchone()[0]==1


@pytest.mark.parametrize('boundary',['git','namespace','delete_before_sql','delete_after_sql'])
def test_sigkill_multifile_and_cleanup_outcomes_are_explicit(env,tmp_path,boundary):
    from app.projects import project_repo_dir
    p=store.create_project('recovery-'+boundary.replace('_','-'))
    repo=project_repo_dir(p,tmp_path/'workspace/proofs')
    GitStore(repo.parent).init_session(repo.name)
    source=f'namespace {p["namespace"]}\ntheorem p : True := by trivial\nend {p["namespace"]}\n'
    (repo/'P.lean').write_text(source)
    if boundary=='git':
        receipt=queue.admit('project.save',{'project_id':p['id'],'request':{'path':'notes.txt','content':'SQL survives Git interruption'}},expected_revision='missing',strict=True)
    elif boundary=='namespace':
        receipt=queue.admit('project.identity',{'slug':p['slug'],'request':{'mode':'rename-namespace','project_name':'After','namespace':'Lea.After','expected_namespace':p['namespace']}})
    else:
        receipt=queue.admit('project.delete',{'project_id':p['id']})
    script=r'''
import os,signal,sys
from pathlib import Path
from app import db,commands,store,workspace_commands as q
from app.routes import sessions,projects
from app.config import LeaConfig
from app.gitstore import GitStore
db.DB_PATH=Path(sys.argv[1]);root=Path(sys.argv[2]);boundary=sys.argv[3]
sessions.load_config=projects.load_config=lambda:LeaConfig(model='test',lea_root=root,max_turns=3)
import app.config
app.config.load_config=sessions.load_config
def die(*a,**k):os.kill(os.getpid(),signal.SIGKILL)
if boundary=='git':
    original=GitStore.commit_all
    def commit(*a,**k):original(*a,**k);die()
    GitStore.commit_all=commit
elif boundary=='namespace':
    from app import workspace_effects
    original=workspace_effects.install_from_snapshot
    def write(path,*a,**k):
        value=original(path,*a,**k)
        if path.suffix=='.lean':die()
        return value
    workspace_effects.install_from_snapshot=write
else:
    original=store.delete_project_cascade
    def delete(*a,**k):
        if boundary=='delete_before_sql':die()
        value=original(*a,**k);die()
    store.delete_project_cascade=delete
epoch=commands.start_worker('killed-workspace-effect')
token=commands.claim('killed-workspace-effect',epoch,'compute',kinds=q.KINDS)
q.execute(token)
'''
    child=subprocess.run([sys.executable,'-c',script,str(db.DB_PATH),str(tmp_path),boundary],capture_output=True,text=True,timeout=30)
    assert child.returncode==-signal.SIGKILL,child.stdout+child.stderr
    workspace_files.recover();workspace_effects.recover();queue.recover_at_startup()
    result=queue.get(receipt['command']['id'])['command']
    if boundary=='git':
        assert result['state']=='suspended'
        assert (repo/'notes.txt').read_text()=='SQL survives Git interruption'
        with db.connect() as conn:
            assert conn.execute("select materialization from workspace_files where path='notes.txt'").fetchone()[0]=='provenance_pending'
    elif boundary=='namespace':
        assert result['state']=='suspended'
        with db.connect() as conn:
            effect=conn.execute("select * from workspace_effects where kind='operation'").fetchone()
            assert effect['state']=='conflict'
            import base64
            entry=json.loads(effect['manifest_json'])['files']['P.lean']
            assert base64.b64decode(store.blob_content(entry['blob_id'])).decode()==source
        assert 'Lea.After' in (repo/'P.lean').read_text()
    elif boundary=='delete_before_sql':
        assert result['state']=='suspended' and store.get_project(p['id'])
        assert (repo/'P.lean').read_text()==source
    else:
        assert result['state']=='succeeded' and store.get_project(p['id']) is None
        tombstone=tmp_path/'workspace/proofs/.lea-trash'/receipt['command']['id']
        assert tombstone.joinpath('P.lean').read_text()==source
