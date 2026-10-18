"""Stage 6 authority contracts, including rejection without workspace access."""
import json
import hashlib
from pathlib import Path
from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from app import db,store,source_publications as source,companion_import,peripheral_views,commands,settings_policy
from app.source_context import source_hash


@pytest.fixture
def database(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'test.sqlite3');db.init_db()
    return tmp_path


def bundle(statement='Statement'):
    value={'version':2,'targetKey':'target','targetKind':'theorem','statement':statement,'proof':'Proof',
        'proofAssociation':{'status':'associated','method':'adjacent','sourceFile':'main.tex','proofHash':hashlib.sha256(b'Proof').hexdigest()},
        'statementLocation':{'sourceFile':'main.tex'},'uses':[],'context':'','relevantSource':[],'mirror':None}
    value['bundleHash']=source_hash(value);return value


def publish(sequence=1,**changes):
    source.publisher(source.PublisherRequest(document_id='document',client_id='tab'))
    return source.publish(source.PublicationRequest(document_id='document',client_id='tab',generation=1,sequence=sequence,
        source_bundle=bundle(**changes),files={'main.tex':'immutable editor bytes'}))


def test_publication_is_sql_only_and_replay_keeps_original(database,monkeypatch):
    monkeypatch.setattr(Path,'read_bytes',lambda *_:pytest.fail('Publication touched workspace'))
    first=publish();assert publish()==first
    with db.connect() as conn:
        assert source.resolve(conn,first['source_context_ref'])['statement']=='Statement'
        assert conn.execute('select count(*) from source_publications').fetchone()[0]==1
    with pytest.raises(HTTPException):publish(statement='different')


def test_unchanged_publication_renews_without_duplicating_bytes(database):
    first=publish();second=publish(2)
    assert first['source_context_ref']==second['source_context_ref']
    with db.connect() as conn:
        assert conn.execute('select count(*) from source_publications').fetchone()[0]==1
        assert conn.execute('select count(*) from source_publication_receipts').fetchone()[0]==2


def test_v2_fragment_and_mirror_order_survive_publication(database):
    from app.source_context import validate_source
    value=bundle();files={'main.tex':'Statement\nProof'}
    value['relevantSource']=[{'kind':'surrounding-context','path':'main.tex','startLine':1,'endLine':2,'content':'Statement\nProof','truncated':False}]
    revision=hashlib.sha256(('main.tex\0'+hashlib.sha256(files['main.tex'].encode()).hexdigest()).encode()).hexdigest()
    value['mirror']={'revision':revision,'mirroredFileCount':1,'verified':True};value['bundleHash']=source_hash(value)
    source.publisher(source.PublisherRequest(document_id='doc',client_id='tab'))
    accepted=source.publish(source.PublicationRequest(document_id='doc',client_id='tab',generation=1,sequence=1,source_bundle=value,files=files,coverage='complete'))
    with db.connect() as conn:
        restored=source.resolve(conn,accepted['source_context_ref'])
        assert validate_source(restored)['bundleHash']==value['bundleHash']
    other=dict(value,targetKey='other');other['bundleHash']=source_hash(other)
    again=source.publish(source.PublicationRequest(document_id='doc',client_id='tab',generation=1,sequence=2,source_bundle=other,archive_ref=accepted['source_archive_ref'],coverage='complete'))
    assert again['source_archive_ref']==accepted['source_archive_ref']


def test_stale_reference_and_second_tab_are_rejected(database):
    first=publish();publish(2,statement='changed')
    with db.connect() as conn:
        with pytest.raises(ValueError):source.resolve(conn,first['source_context_ref'])
    with pytest.raises(HTTPException):source.publisher(source.PublisherRequest(document_id='document',client_id='other'))
    with db.write() as conn:conn.execute("update source_publishers set lease_until='2000-01-01'")
    with pytest.raises(HTTPException):source.publisher(source.PublisherRequest(document_id='document',client_id='other'))
    claim=source.publisher(source.PublisherRequest(document_id='document',client_id='other',expected_generation=1))
    assert claim['generation']==2
    with pytest.raises(HTTPException):publish(3)


def test_publication_and_domain_command_roll_back_together(database):
    with pytest.raises(RuntimeError):
        with db.write():
            publish()
            raise RuntimeError('admission rejected')
    with db.connect() as conn:assert not conn.execute('select 1 from source_publications').fetchone()


def test_concurrent_publisher_has_one_owner(database):
    def claim(client):
        try:return source.publisher(source.PublisherRequest(document_id='doc',client_id=client))
        except HTTPException:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(claim,['one','two']))
    assert sum(r is not None for r in results)==1


def test_legacy_import_is_idempotent_redacted_and_does_not_certify(database):
    legacy=database/'legacy';legacy.mkdir()
    jobs={'old':{'jobId':'old','status':'formalized','apiRunId':'missing','apiKey':'sk-do-not-import-this-key'}}
    path=legacy/'jobs.json';path.write_text(json.dumps(jobs))
    result=companion_import.import_directory(legacy)
    assert result['jobs.json']['issues']==1
    assert companion_import.import_directory(legacy)=={}
    assert json.loads(path.read_text())==jobs
    with db.connect() as conn:
        assert conn.execute('select count(*) from companion_imports').fetchone()[0]==1
        assert conn.execute('select count(*) from companion_issues').fetchone()[0]==1
        assert not conn.execute('select 1 from runs').fetchone()
        assert 'sk-do-not-import' not in conn.execute('select payload from companion_imports').fetchone()[0]
    path.write_text(json.dumps({'new':{'jobId':'new'}}));companion_import.import_directory(legacy)
    with db.connect() as conn:assert conn.execute("select count(*) from companion_issues where reason='legacy_delta_after_freeze'").fetchone()[0]==1


def test_peripheral_get_cannot_invoke_reader(database):
    calls=[]
    @peripheral_views.query('test_bounded_reader')
    def reader():calls.append('worker');return {'data':'retained'}
    with pytest.raises(HTTPException):reader()
    assert calls==[]
    peripheral_views.capture('test_bounded_reader',{})
    assert reader()['data']=='retained' and calls==['worker']


def test_database_download_survives_missing_workspace(database):
    from fastapi.responses import Response
    @peripheral_views.query('test_export',automatic=False)
    def exporter():return Response(b'archive bytes',media_type='application/zip',headers={'Content-Disposition':'attachment; filename="proof.zip"'})
    result=peripheral_views.capture('test_export',{})
    assert peripheral_views.download(result['$download']).body==b'archive bytes'


def test_old_worker_epoch_cannot_admit_database_writes(database):
    commands.start_worker('lea-command-host')
    marker=db.caller_worker_epoch.set('old-epoch')
    try:
        with pytest.raises(HTTPException):
            with db.write() as conn:conn.execute("insert into companion_documents values('bad','{}',1,'now')")
    finally:db.caller_worker_epoch.reset(marker)


def test_credentials_have_only_infrastructure_reference(database):
    ticket=settings_policy.stage_credentials({'api_keys':{'OPENAI_API_KEY':{'value':'sk-private-test-value'}}})
    values,path=settings_policy.consume(ticket)
    assert path.stat().st_mode&0o777==0o600
    assert values['api_keys']['OPENAI_API_KEY']['value']=='sk-private-test-value'
    with db.connect() as conn:assert not conn.execute('select 1 from commands').fetchone()
    path.unlink()


def test_accepted_business_intent_keeps_source_when_browser_lease_expires(database):
    from app import companion_service
    first=publish()
    accepted=companion_service.admit('/formalize',{'overleafProjectId':'document','source_context_ref':first['source_context_ref']},'accepted')
    epoch=commands.start_worker('lea-command-host')
    token=commands.claim('lea-command-host',epoch,'compute',kinds={'companion.dispatch'})
    with db.write() as conn:conn.execute("update source_publishers set lease_until='2000-01-01'")
    with db.connect() as conn:
        with pytest.raises(ValueError):source.resolve(conn,first['source_context_ref'])
    marker=db.caller_parent_command.set(token['id'])
    try:
        with db.connect() as conn:assert source.resolve(conn,first['source_context_ref'])['statement']=='Statement'
    finally:db.caller_parent_command.reset(marker)
    duplicate=companion_service.admit('/formalize',{'overleafProjectId':'document','source_context_ref':first['source_context_ref']},'accepted')
    assert duplicate['command']['id']==accepted['command']['id']


def test_archive_bytes_are_shared_but_a_changed_archive_fences_old_source(database):
    first=publish()
    other=bundle();other['targetKey']='other';other['bundleHash']=source_hash(other)
    source.publish(source.PublicationRequest(document_id='document',client_id='tab',generation=1,sequence=2,source_bundle=other,files={'main.tex':'immutable editor bytes'}))
    with db.connect() as conn:assert conn.execute("select count(*) from source_archives where mode='publication'").fetchone()[0]==1
    source.publish(source.PublicationRequest(document_id='document',client_id='tab',generation=1,sequence=3,source_bundle=bundle(),files={'main.tex':'changed surrounding input'}))
    with db.connect() as conn:
        with pytest.raises(ValueError):source.resolve(conn,first['source_context_ref'])


def test_imported_pending_batch_is_retained_without_execution_authority(database):
    root=database/'old';root.mkdir()
    (root/'jobs.json.batches.json').write_text(json.dumps({'b':{'batchId':'b','operation':'formalize','done':False,'items':[{'state':'pending'}]}}))
    companion_import.import_directory(root)
    with db.connect() as conn:batch=json.loads(conn.execute("select payload from companion_documents where name='jobs.json.batches.json'").fetchone()[0])['b']
    assert batch['migrationBlocked'] and batch['pausedOn']['reason']=='migration_review'
    assert (root/'.lea-db-migration.json').exists()
    with db.connect() as conn:assert conn.execute('select count(*) from runs').fetchone()[0]==0


def test_nested_failure_rolls_back_only_its_savepoint(database):
    with db.write() as conn:
        conn.execute("insert into companion_documents values('outer','{}',1,'now')")
        with pytest.raises(ValueError):
            with db.write() as inner:
                inner.execute("insert into companion_documents values('inner','{}',1,'now')")
                raise ValueError('reject this nested admission')
        assert conn.execute("select name from companion_documents").fetchone()[0]=='outer'
    with db.connect() as conn:assert conn.execute('select count(*) from companion_documents').fetchone()[0]==1


def test_all_registered_application_gets_are_sql_only(database,monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import filesystem,projects,catalog_service,registry_backfill,config,project_coordination
    from app.gitstore import GitStore
    import subprocess
    project=store.create_project('sql-boundary')
    session=store.create_session('SQL session',project_id=project['id'])
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Target')
    run=store.create_run(session['id'],'history','test',3);store.update_run(run['id'],'cancelled')
    def denied(*args,**kwargs):pytest.fail('An application GET crossed the database boundary')
    for module,names in [(filesystem,['build_tree','safe_abs','read_text_file','export_zip']),
                         (projects,['read_doc']), (project_coordination,['snapshot']), (catalog_service,['service']),
                         (registry_backfill,['backfill_artifacts_from_registry']), (config,['load_config'])]:
        for name in names:monkeypatch.setattr(module,name,denied)
    monkeypatch.setattr(GitStore,'__init__',denied);monkeypatch.setattr(subprocess,'Popen',denied)
    # Route modules previously imported load_config by name too.
    import sys
    for name,module in list(sys.modules.items()):
        if name.startswith('app.routes.') and hasattr(module,'load_config'):monkeypatch.setattr(module,'load_config',denied)
    substitutions={'project_id':project['id'],'slug':project['slug'],'session_id':session['id'],
        'formalization_id':form['id'],'run_id':run['id'],'name':'missing','path':'Target.lean'}
    credential=commands.register_producer('query-reader')
    from app.editor_service import create_client
    editor_credential=create_client(session['id'])['credential']
    checked=[];client=TestClient(app,base_url='http://localhost')
    for route in app.routes:
        template=getattr(route,'path','')
        if 'GET' not in getattr(route,'methods',set()) or not template.startswith('/api/'):continue
        if template.endswith('/events'):continue # streaming uses SQL replay; tested separately
        url=template
        import re
        for match in re.finditer(r'\{([^}:]+)(?::[^}]+)?\}',template):url=url.replace(match[0],substitutions.get(match[1],'missing'))
        response=client.get(url,headers={'Authorization':'Bearer '+(editor_credential if template.startswith('/api/editor/') else credential)},params={'path':'Target.lean','project_id':project['id'],'model':'test','idempotency_key':'missing','q':'Target'})
        assert response.status_code in {200,404,409,410,422,503},(template,response.status_code,response.text)
        checked.append(template)
    assert len(checked)>=60


def test_source_materialization_retains_sql_and_external_edits(database,monkeypatch):
    from app import run_commands
    from app.config import LeaConfig
    from app.projects import project_repo_dir
    import app.config
    cfg=LeaConfig(model='test',lea_root=database,max_turns=3)
    monkeypatch.setattr(app.config,'load_config',lambda:cfg)
    project=store.create_project('Document')
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Target',
        origin='overleaf',origin_key='document:theorem:target')
    publication=publish()
    created=store.create_run_bundle(message='test',session_id=None,project_id=project['id'],session_origin='overleaf',
        session_origin_url=None,model='test',provider=None,max_turns=3,autonomous=True,
        focus_formalization_id=form['id'],source_bundle=bundle(),source_context_ref=publication['source_context_ref'])
    epoch=commands.start_worker('test');token=commands.claim('test',epoch,'compute',kinds={'run.execute'})
    repo=project_repo_dir(project,database/'workspace/proofs');repo.mkdir(parents=True)
    with run_commands.executing(token):assert source.materialize_run(created['run']['id'],database/'workspace/proofs')
    target=repo/'.lea/files/overleaf/main.tex'
    assert target.read_text()=='immutable editor bytes'
    with db.connect() as conn:
        assert conn.execute("select count(*) from workspace_effects where state='published'").fetchone()[0]==1
        binding=conn.execute("select blob_id from workspace_files where path='.lea/files/overleaf/main.tex'").fetchone()
        assert store.blob_content(binding[0])=='immutable editor bytes'
    target.write_text('external editor wins')
    with run_commands.executing(token),pytest.raises(ValueError,match='external edit'):
        source.materialize_run(created['run']['id'],database/'workspace/proofs')
    assert target.read_text()=='external editor wins'


def test_source_retention_keeps_heads_and_run_provenance(database):
    first=publish();publish(2,statement='current')
    with db.write() as conn:conn.execute("update source_publications set created_at='2000-01-01'")
    assert source.retain()==1
    with db.connect() as conn:
        assert conn.execute('select count(*) from source_publications').fetchone()[0]==1
        assert not conn.execute('select 1 from source_publications where id=?',(first['source_context_ref'],)).fetchone()


def test_batch_cancellation_is_an_admission_barrier(database):
    from app import companion_service
    with db.write() as conn:conn.execute("insert into companion_documents values('jobs.json.batches.json',?,1,?)",(json.dumps({'batch':{'batchId':'batch','overleafProjectId':'document'}}),db.utc_now()))
    companion_service.admit('/lean-pane/repair/all/cancel',{'batchId':'batch'},'cancel')
    marker=db.caller_batch.set('batch')
    try:
        with pytest.raises(HTTPException,match='cancelled'):
            with db.write():pass
    finally:db.caller_batch.reset(marker)


def test_namespace_generation_survives_aba_and_is_not_a_process_counter(database,monkeypatch):
    from app.queries.identity import project_identity
    from app import project_coordination
    project=store.create_project('namespace-version')
    initial=project_identity(project)
    monkeypatch.setattr(project_coordination,'snapshot',lambda *_:pytest.fail('SQL identity read a process counter'))
    with db.write() as conn:
        conn.execute("update projects set namespace='Lea.New',repo_path='Lea/New' where id=?",(project['id'],))
        conn.execute('update projects set namespace=?,repo_path=? where id=?',(project['namespace'],project['repo_path'],project['id']))
    final=project_identity(project)
    assert final['namespace']==initial['namespace']
    assert final['namespaceRevision']==initial['namespaceRevision']+2


def test_authorized_git_publication_is_queued_and_replays_without_a_second_push(database,monkeypatch):
    import subprocess
    from app import workspace_commands as queue,projects
    from app.routes import projects as routes
    from app.config import LeaConfig
    config=LeaConfig(model='fixture',max_turns=3,lea_root=database)
    monkeypatch.setattr(routes,'load_config',lambda:config)
    project=projects.provision_project('Queued publication',database/'workspace/proofs')
    repo=projects.project_repo_dir(project,database/'workspace/proofs')
    (repo/'hello.txt').write_text('Approved local publication\n')
    remote=database/'owned-remote.git'
    subprocess.run(['git','init','--bare','-q',str(remote)],check=True)
    store.update_project(project['id'],remote_url=str(remote))
    monkeypatch.setattr(routes,'github_token',lambda:'fixture-token')
    from app.gitstore import GitStore
    push=GitStore.push_to_github
    monkeypatch.setattr(GitStore,'push_to_github',lambda *a,**k:pytest.fail('Intake pushed Git'))
    accepted=queue.admit('project.push_slug',{'slug':project['slug']},idempotency_key='authorized-local-share')
    assert accepted['command']['state']=='queued'
    monkeypatch.setattr(GitStore,'push_to_github',push)
    epoch=commands.start_worker('publication-test')
    token=commands.claim('publication-test',epoch,'compute',kinds=queue.KINDS)
    assert queue.execute(token)
    result=queue.get(token['id'])['command']
    assert result['state']=='succeeded',result
    assert result['result']['pushed']
    assert subprocess.run(['git','--git-dir',str(remote),'show','main:hello.txt'],capture_output=True,text=True,check=True).stdout=='Approved local publication\n'
    assert queue.admit('project.push_slug',{'slug':project['slug']},idempotency_key='authorized-local-share')['command']['id']==token['id']
    assert commands.claim('publication-test',epoch,'compute',kinds=queue.KINDS) is None


def test_bounded_peripheral_maintenance_eventually_visits_older_scopes(database,monkeypatch):
    monkeypatch.setattr(peripheral_views,'_queries',{})
    seen=set()
    @peripheral_views.query('fair_scope')
    def reader(project_id):
        seen.add(project_id)
        return {'project_id':project_id}
    wanted={store.create_project('scope-'+str(i))['id'] for i in range(110)}
    for _ in range(37):assert peripheral_views.maintain(limit=3)<=3
    assert seen==wanted


def test_run_completion_cannot_promote_an_unpublished_repair(database):
    from app.routes.companion import job
    session=store.create_session('Repair history')
    run=store.create_run(session['id'],'fixture',None,3)
    store.update_run(run['id'],'proved',result_kind='proved')
    payload={'mode':'repair','status':'needs_review','reformalizationId':'retained-candidate','finishedAt':'now'}
    with db.write() as conn:
        conn.execute('insert into companion_jobs values(?,?,?,?,?,?,?,?,?)',('repair','doc','doc:theorem:t',None,session['id'],run['id'],None,json.dumps(payload),db.utc_now()))
    value=job('repair')
    assert value['domain_run_status']=='proved'
    assert value['status']=='needs_review'


def test_metadata_commands_do_not_require_a_live_source_publisher(database):
    from app import companion_service
    publish()
    with db.write() as conn:conn.execute("update source_publishers set lease_until='2000-01-01'")
    result=companion_service.admit('/project/identity',{'overleafProjectId':'document','projectName':'Renamed'},'rename-without-tab','PUT')
    assert result['command']['state']=='queued'
    with pytest.raises(HTTPException):
        companion_service.admit('/formalize',{'overleafProjectId':'document','targetLabel':'target'},'formalize-without-current-source')


def test_business_children_cannot_extend_the_accepted_deadline(database):
    from app import companion_service,run_commands
    accepted=companion_service.admit('/project/identity',{'overleafProjectId':'document','projectName':'Name'},'parent','PUT')['command']
    until=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
    with db.write() as conn:conn.execute("update commands set state='succeeded',deadline=? where id=?",(until,accepted['id']))
    marker=db.caller_parent_command.set(accepted['id'])
    try:
        with db.write() as conn:
            child=run_commands._enqueue(conn,'synthetic.echo',{'text':'child'},key='child')
            parent_end=conn.execute('select deadline from commands where id=?',(accepted['id'],)).fetchone()[0]
            assert conn.execute('select deadline from commands where id=?',(child['id'],)).fetchone()[0]==parent_end==until
        with db.write() as conn:conn.execute("update commands set deadline='2000-01-01' where id=?",(accepted['id'],))
        with pytest.raises(commands.QueueError,match='parent_deadline_expired'):
            with db.write() as conn:run_commands._enqueue(conn,'synthetic.echo',{'text':'late'},key='late-child')
    finally:db.caller_parent_command.reset(marker)


def test_lost_business_ack_replays_after_the_first_run_resolves_its_project(database):
    from app import companion_service
    published=publish()
    payload={'overleafProjectId':'document','source_context_ref':published['source_context_ref']}
    accepted=companion_service.admit('/formalize',payload,'lost-first-ack')['command']
    project=store.create_project('document')
    result={'statusCode':200,'body':{'jobId':'original-job'}}
    with db.write() as conn:
        conn.execute("update commands set project_id=?,state='succeeded' where id=?",(project['id'],accepted['id']))
        conn.execute('update companion_requests set result_json=? where command_id=?',(json.dumps(result),accepted['id']))
    replay=companion_service.admit('/formalize',payload,'lost-first-ack')['command']
    assert replay['id']==accepted['id'] and replay['result']==result
    with pytest.raises(commands.QueueError,match='idempotency_conflict'):
        companion_service.admit('/formalize',{**payload,'targetLabel':'changed'},'lost-first-ack')


def test_imported_foreign_document_association_is_history_without_execution_authority(database):
    project=store.create_project('right-document')
    session=store.create_session('Canonical right document',project_id=project['id'])
    legacy=database/'foreign-legacy';legacy.mkdir()
    (legacy/'jobs.json').write_text(json.dumps({'wrong':{'jobKey':'wrong-document:theorem:t','overleafProjectId':'wrong-document','leaSessionId':session['id']}}))
    companion_import.import_directory(legacy)
    with db.connect() as conn:
        assert conn.execute("select reason from companion_issues").fetchone()[0]=='conflicting_document_reference'
        assert not conn.execute("select 1 from companion_jobs where id='wrong'").fetchone()
        assert 'wrong' in json.loads(conn.execute("select payload from companion_documents where name='jobs.json'").fetchone()[0])


def test_child_admission_cannot_cross_its_business_project(database):
    from app import companion_service,run_commands
    left=store.create_project('left');right=store.create_project('right')
    accepted=companion_service.admit('/project/identity',{'overleafProjectId':'left','projectName':'Name'},'scoped-parent','PUT')['command']
    with db.write() as conn:conn.execute("update commands set state='succeeded' where id=?",(accepted['id'],))
    marker=db.caller_parent_command.set(accepted['id'])
    try:
        with pytest.raises(commands.QueueError,match='parent_scope_mismatch'):
            with db.write() as conn:run_commands._enqueue(conn,'synthetic.echo',{'text':'wrong project'},key='foreign-child',project_id=right['id'])
    finally:db.caller_parent_command.reset(marker)


def test_malformed_legacy_reference_is_retained_as_an_issue(database):
    legacy=database/'malformed-legacy';legacy.mkdir()
    original={'bad-id':{'leaSessionId':{'untrusted':'value'}},'bad-key':{'jobKey':['invalid']}}
    (legacy/'jobs.json').write_text(json.dumps(original))
    result=companion_import.import_directory(legacy)
    assert result['jobs.json']['issues']==2
    with db.connect() as conn:
        assert conn.execute('select count(*) from companion_jobs').fetchone()[0]==0
        assert json.loads(conn.execute('select payload from companion_imports').fetchone()[0])==original
    assert json.loads((legacy/'jobs.json').read_text())==original


def test_nonfinite_legacy_json_cannot_break_the_worker_decoder(database):
    legacy=database/'nonfinite';legacy.mkdir()
    (legacy/'jobs.json').write_text('{"bad":{"costUsd":NaN}}')
    assert companion_import.import_directory(legacy)['jobs.json']['issues']==1
    with db.connect() as conn:
        assert conn.execute('select reason from companion_issues').fetchone()[0]=='invalid_legacy_json'
        assert not conn.execute("select 1 from companion_documents where name='jobs.json'").fetchone()


def test_redispatch_cannot_publish_a_terminal_event_for_a_running_solver(database,monkeypatch):
    from app import bridge,runbroker,runregistry,runtime,run_commands
    monkeypatch.setattr(runtime,'accepting_work',lambda:True)
    monkeypatch.setattr(run_commands,'drain_requested',lambda:False)
    session=store.create_session('Still running')
    run=store.create_run(session['id'],'fixture',None,3)
    store.claim_pending_run(run['id'])
    broker=runbroker.create(run['id'])
    try:
        assert bridge._try_dispatch(run['id'],{})==bridge._SETTLED
        bridge.publish_terminal_from_row(run['id'])
        assert runbroker.get(run['id']) is broker and not broker.closed
        assert broker.events_after(0)==[]
        assert store.get_run(run['id'])['status']=='running'
    finally:runbroker.drop(run['id'])


def test_legacy_nonterminal_done_does_not_hide_the_canonical_result(database):
    from fastapi.testclient import TestClient
    from app import observation
    from app.main import app
    session=store.create_session('Retained replay')
    run=store.create_run(session['id'],'fixture',None,3)
    store.claim_pending_run(run['id'])
    observation.append_run_event(run['id'],'done',{'status':'running'})
    store.update_run(run['id'],'proved',result_kind='proved')
    response=TestClient(app,base_url='http://localhost').get('/api/runs/'+run['id']+'/events')
    frames=[json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert frames[-1]['status']=='proved'
    assert all(frame.get('status')!='running' for frame in frames)
    with db.connect() as conn:assert conn.execute("select count(*) from run_events where type='done'").fetchone()[0]==1


def test_partial_repair_drafts_are_observable_before_an_assessment_exists(database,monkeypatch):
    from app import core_observer,formalizations,reformalizations,reformalization_review,artifact_snapshots,read_projections
    form=store.create_formalization(project_id=None,loose_session_id=store.create_session('Draft owner')['id'],display_title='Target')
    monkeypatch.setattr(formalizations,'get',lambda *_:{**form,'review':{},'completion_run_id':None})
    monkeypatch.setattr(artifact_snapshots,'_snapshot',lambda *_:None)
    monkeypatch.setattr(reformalization_review,'latest',lambda *_:{'id':'active-repair','state':'active','root_run_id':'run','result':None})
    monkeypatch.setattr(reformalization_review,'binding',lambda *_:('token',{}))
    monkeypatch.setattr(reformalizations,'drafts',lambda *_:[{'path':'P.lean','blob_id':'retained','evidence':{}}])
    monkeypatch.setattr(core_observer,'_target',lambda *_:{'exists':False})
    value=core_observer._capture('formalization',form['id'],read_projections.ReadSnapshot())
    assert value['review_context']['review']['eligible'] is False
    assert value['review_context']['artifacts'][0]['blob_id']=='retained'


def test_new_observations_are_prioritized_without_starving_expiring_views(database,monkeypatch):
    from app import core_observer
    from app.queries import core
    old=store.create_project('old-observed');new=store.create_project('new-cold')
    older='p:'+old['id'];cold='p:'+new['id']
    core_observer.enroll(older);core_observer.enroll(cold)
    now=datetime.now(timezone.utc)
    with db.write() as conn:
        conn.execute('insert into core_observations values(?,?,?,?,?,?,?,?,?)',('healthy',older,core.revision(conn,older),0,
            (now-timedelta(seconds=40)).isoformat(),(now+timedelta(seconds=80)).isoformat(),'observed','','{}'))
        conn.execute('update core_projection_enrollment set observation_id=?,last_scan_at=? where scope=?',('healthy',(now-timedelta(seconds=40)).isoformat(),older))
        conn.execute('update core_projection_enrollment set last_scan_at=? where scope=?',((now-timedelta(seconds=1)).isoformat(),cold))
    selected=[]
    monkeypatch.setattr(core_observer,'capture',lambda scope:selected.append(scope) or {'state':'observed'})
    core_observer.maintain();assert selected==[cold]
    with db.write() as conn:conn.execute('update core_observations set expires_at=? where id=?',((now+timedelta(seconds=10)).isoformat(),'healthy'))
    core_observer.maintain();assert selected==[cold,older]
    with db.write() as conn:
        conn.execute('insert into core_observations values(?,?,?,?,?,?,?,?,?)',('cold-completed',cold,core.revision(conn,cold),0,
            now.isoformat(),(now+timedelta(seconds=120)).isoformat(),'observed','','{}'))
        conn.execute('update core_projection_enrollment set observation_id=?,last_scan_at=? where scope=?',('cold-completed',now.isoformat(),cold))
        conn.execute('update core_observations set expires_at=? where id=?',((now+timedelta(seconds=80)).isoformat(),'healthy'))
        conn.execute('insert into core_scope_revisions values(?,1) on conflict(scope) do update set revision=revision+1',(cold,))
    core_observer.maintain();assert selected==[cold,older,cold]
