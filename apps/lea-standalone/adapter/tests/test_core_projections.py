"""Enrolled core views keep proof semantics with filesystem access denied."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import db,store,core_observer,proof_evidence,formalizations
from app.config import LeaConfig
from app.queries import core,proofs,sessions
from app.routes import sessions as session_routes,formalizations as form_routes,lea_status,projects,project_library
from evidence_fixtures import mock_execution


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'data/db.sqlite')
    db.init_db()
    project=store.create_project('observed',namespace='Lea.Observed')
    session=store.create_session('Session',project_id=project['id'])
    fid=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Result',declaration_name='result')['id']
    store.link_session_formalization(session['id'],fid)
    store.link_formalization_file(fid,'Proof.lean',role='primary')
    run=store.create_run(session['id'],'mock',None,3,focus_formalization_id=fid)
    root=tmp_path/'prover';workspace=root/'workspace';repo=workspace/'proofs/Lea/Observed';repo.mkdir(parents=True)
    (workspace/'lakefile.toml').write_text('name="fixture"\n')
    path=repo/'Proof.lean';path.write_text('theorem result : True := by trivial\n')
    monkeypatch.setattr('app.config.load_config',lambda:LeaConfig(model='mock',max_turns=3,lea_root=root))
    store.add_code_step(session['id'],run['id'],'Proof.lean',content=path.read_text(),formalization_id=fid)
    from lea.events import CheckResult
    ok,step=proof_evidence.record_check(session['id'],'Proof.lean',path,
        CheckResult(str(path),'ok',execution=mock_execution(path),content=path.read_text()),run_id=run['id'],formalization_id=fid)
    assert ok
    store.update_run(run['id'],'proved',result_kind='proved',result_detail='PROVED')
    scope='p:'+project['id'];core_observer.enroll(scope)
    return {'project':project,'session':session,'fid':fid,'run':run,'scope':scope,'root':root,'path':path,'step':step}


def finish(x,limit=20):
    for _ in range(100):
        result=core_observer.capture(x['scope'],limit=limit)
        if result['state']=='observed':return result
    pytest.fail('Core backfill did not settle')


def app():
    value=FastAPI()
    for route in (session_routes,form_routes,lea_status,projects,project_library):value.include_router(route.router)
    return value


def test_fresh_projection_matches_legacy_and_all_core_http_reads_are_sql_only(fixture,monkeypatch):
    x=fixture
    expected=formalizations.get(x['fid'])
    finish(x,limit=1)
    assert proofs.get(x['fid'])['validity_status']==expected['validity_status']=='proved'
    def denied(*a,**kw):raise AssertionError('GET entered a workspace/prover service')
    monkeypatch.setattr('app.read_projections.ReadSnapshot',denied)
    monkeypatch.setattr(store,'session_detail',denied)
    monkeypatch.setattr(formalizations,'get',denied)
    monkeypatch.setattr(Path,'read_bytes',denied)
    monkeypatch.setattr(Path,'read_text',denied)
    with TestClient(app()) as client:
        paths=['/api/sessions','/api/stats','/api/projects',f"/api/projects/{x['project']['id']}",f"/api/sessions/{x['session']['id']}",f"/api/sessions/{x['session']['id']}/formalizations",
            f"/api/formalizations/{x['fid']}",f"/api/formalizations/{x['fid']}/current",f"/api/formalizations/{x['fid']}/review",
            f"/api/projects/{x['project']['id']}/formalizations",'/api/projects/by-slug/observed/formalizations',
            '/api/projects/by-slug/observed/target-status?declarations=result',f"/api/formalizations/{x['fid']}/lea-status",
            f"/api/formalizations/{x['fid']}/lea-status/updates",'/api/projects/by-slug/observed/dependencies',
            '/api/projects/by-slug/observed/library']
        for path in paths:
            response=client.get(path)
            assert response.status_code==200,(path,response.text)
        detail=client.get(f"/api/sessions/{x['session']['id']}").json()
        assert detail['code_steps'][0]['check_current'] is True
        assert detail['formalizations'][0]['certification']['source']=='run'


@pytest.mark.parametrize('change',['source','delete','config','dependency','toolchain','nearer_root'])
def test_worker_detects_external_changes_without_watcher_and_retains_history(fixture,change):
    x=fixture
    if change in {'dependency','toolchain'}:
        dependency=x['path'].parent/('Dependency.lean' if change=='dependency' else 'lean-binary')
        dependency.write_text('old')
        with db.write() as conn:
            row=conn.execute('select evidence_json from proof_check_evidence where id=?',(x['step']['check_evidence_id'],)).fetchone()
            execution=json.loads(row[0])
            from lea.check_evidence import digest,environment_hash
            execution['environment']['files']['external']={'path':str(dependency),'sha256':digest(dependency.read_bytes()),'kind':'source'}
            execution['environment_sha256']=environment_hash(execution['environment'])
            conn.execute('update proof_check_evidence set evidence_json=? where id=?',(json.dumps(execution),x['step']['check_evidence_id']))
    finish(x)
    assert proofs.get(x['fid'])['check_current']
    if change=='source':x['path'].write_text('theorem result : True := by sorry\n')
    elif change=='delete':x['path'].unlink()
    elif change in {'dependency','toolchain'}:dependency.write_text('new')
    elif change=='config':(x['root']/'workspace/lakefile.toml').write_text('name="changed"\n')
    else:(x['path'].parent/'lakefile.toml').write_text('name="nearer"\n')
    finish(x)
    form=proofs.get(x['fid'])
    assert not form['check_current'] and form['certification'] is None
    step=proofs.current_snapshot(x['fid'])['files'][0]
    assert step['code']=='theorem result : True := by trivial\n' and step['check_status']=='ok'
    assert not step['check_current']
    target=proofs.target_status(x['project'],'result')['targets'][0]
    assert target['exists'] is (change!='delete')
    if change=='delete':assert target['content'] is None and not target['declaration_present']
    if change=='source':assert target['has_sorry'] is True and target['declaration_present'] is True


def test_expiry_and_domain_changes_disable_current_evidence_without_blocking_messages(fixture):
    x=fixture;finish(x)
    store.add_message(x['session']['id'],'assistant','live committed text')
    assert proofs.get(x['fid'])['check_current']  # Prose/usage writes do not invalidate proof inputs.
    with db.write() as conn:
        conn.execute('update core_observations set expires_at=?',((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    assert not proofs.get(x['fid'])['check_current']
    assert sessions.session_detail(x['session']['id'])['messages'][-1]['content']=='live committed text'
    finish(x)
    store.update_formalization(x['fid'],statement='Changed mathematical contract')
    assert proofs.get(x['fid'])['projection']['state']=='inputs_changed'
    assert not proofs.get(x['fid'])['review']['eligible']


def test_resume_rejects_mixed_source_versions_and_keeps_old_generation(fixture):
    x=fixture;first=finish(x)
    assert core_observer.capture(x['scope'],limit=1)['state']=='capturing'
    x['path'].write_text('changed')
    # Persisted observations from the earlier partial batch cannot be renewed.
    finish(x,limit=1)
    assert not proofs.get(x['fid'])['check_current']
    with db.connect() as conn:
        assert conn.execute('select count(*) from core_observations where id=?',(first['observation_id'],)).fetchone()[0]==1


def test_scope_move_invalidates_both_scopes_and_query_writes_fail(fixture):
    x=fixture;finish(x)
    other=store.create_project('other')
    with db.connect() as conn: before=core.revision(conn,'p:'+other['id'])
    with db.write() as conn:conn.execute('update sessions set project_id=? where id=?',(other['id'],x['session']['id']))
    with db.connect() as conn: assert core.revision(conn,'p:'+other['id'])>before
    assert not proofs.get(x['fid'])['check_current']
    @core.read_query
    def illegal():
        with db.connect() as conn:conn.execute("update sessions set title='bad'")
    with pytest.raises(Exception,match='readonly'):illegal()


def test_user_review_lineage_is_preserved_and_expiry_never_creates_approval(fixture):
    from app import formalization_reviews
    x=fixture
    store.update_run(x['run']['id'],'needs_review',result_kind='needs_review',result_detail='Check correspondence')
    context=formalization_reviews.context(x['fid'])
    assert context['review']['eligible']
    decision=formalization_reviews.decide(x['fid'],{'action':'approve','outcome':'proved',
        'revision_token':context['review']['revision_token'],'idempotency_key':'approved'})
    finish(x)
    current=proofs.get(x['fid'])
    assert current['certification']['source']=='user_review'
    assert current['review']['approval']['id']==decision['decision']['id']
    with db.write() as conn:conn.execute("update core_observations set expires_at='2000-01-01T00:00:00+00:00'")
    assert proofs.get(x['fid'])['certification'] is None
    assert not proofs.review_context(x['fid'])['review']['eligible']
    with db.connect() as conn:assert conn.execute('select count(*) from formalization_review_events').fetchone()[0]==1
    formalization_reviews.decide(x['fid'],{'action':'withdraw','approval_id':decision['decision']['id'],'idempotency_key':'withdraw'})
    finish(x)
    assert proofs.get(x['fid'])['validity_status']=='needs_review'
    assert sessions.session_detail(x['session']['id'])['review_events'][-1]['action']=='withdraw'


def test_bounded_backfill_and_legacy_log_import_are_persisted_not_get_fallbacks(fixture,monkeypatch,tmp_path):
    x=fixture
    logs=tmp_path/'logs';logs.mkdir()
    monkeypatch.setattr(store,'RAW_EVENT_LOG_DIR',logs)
    (logs/(x['run']['id']+'.jsonl')).write_text(json.dumps({'type':'approval_requested','payload':{'approval_id':'old','lean_code':'retained'}})+'\n')
    for i in range(205):
        step=store.add_code_step(x['session']['id'],None,f'History{i}.lean',content='-- history',formalization_id=x['fid'])
    first=core_observer.capture(x['scope'],limit=20)
    assert first['processed']==20 and first['state']=='capturing'
    assert core.metadata(x['scope'])['state']=='synchronizing'
    finish(x,limit=100)
    assert len(sessions.session_detail(x['session']['id'])['code_steps'])==206
    (logs/(x['run']['id']+'.jsonl')).unlink()
    assert sessions.session_detail(x['session']['id'])['approval_events'][0]['approval_id']=='old'
    with db.connect() as conn:
        assert conn.execute('select count(*) from core_backfills').fetchone()[0]==0
    def denied(*a,**kw):raise AssertionError('raw-log fallback')
    monkeypatch.setattr(store,'approval_events_for_session',denied)
    monkeypatch.setattr(store,'_usage_breakdown_from_raw_logs',denied)
    assert sessions.session_detail(x['session']['id'])['approval_events'][0]['lean_code']=='retained'


def test_blob_corruption_invalidates_facts_in_the_same_transaction(fixture):
    x=fixture;finish(x)
    with db.write() as conn:conn.execute("update artifact_blobs set content='corrupt' where id=?",(x['step']['blob_id'],))
    assert proofs.get(x['fid'])['projection']['state']=='inputs_changed'
    assert proofs.get(x['fid'])['certification'] is None


def test_legacy_usage_import_survives_new_sql_accounting_and_refresh(fixture,monkeypatch):
    x=fixture
    with db.write() as conn:conn.execute("update runs set created_at='2000-01-01' where id=?",(x['run']['id'],))
    newer=store.create_run(x['session']['id'],'mock',None,3)
    store.replace_run_usage_breakdown(newer['id'],[{'phase':'new','input_tokens':7}])
    monkeypatch.setattr(store,'_usage_breakdown_from_raw_logs',lambda sid:[{'run_number':1,'ordinal':1,'phase':'legacy','input_tokens':11}])
    finish(x)
    rows=sessions.usage_breakdown_for_session(x['session']['id'])
    assert [(r['run_id'],r['input_tokens']) for r in rows]==[(x['run']['id'],11),(newer['id'],7)]
    def denied(*a,**kw):raise AssertionError('Legacy archive must be imported once')
    monkeypatch.setattr(store,'_usage_breakdown_from_raw_logs',denied)
    finish(x)
    assert sessions.usage_breakdown_for_session(x['session']['id'])==rows
    # A subsequently persisted replacement for the old run takes precedence.
    store.replace_run_usage_breakdown(x['run']['id'],[{'phase':'legacy','input_tokens':12}])
    assert [r['input_tokens'] for r in sessions.usage_breakdown_for_session(x['session']['id'])]==[12,7]


def test_loose_target_shared_between_conversations_has_one_observed_revision(fixture):
    x=fixture
    owner=store.create_session('Loose owner');chat=store.create_session('Associated chat')
    fid=store.create_formalization(project_id=None,loose_session_id=owner['id'],display_title='Shared',declaration_name='shared')['id']
    store.link_session_formalization(chat['id'],fid);store.link_formalization_file(fid,'Shared.lean','primary')
    path=x['root']/'workspace/proofs'/chat['id']/'Shared.lean';path.parent.mkdir(parents=True)
    path.write_text('theorem shared : True := by trivial\n')
    run=store.create_run(chat['id'],'mock',None,3,focus_formalization_id=fid)
    store.add_code_step(chat['id'],run['id'],'Shared.lean',content=path.read_text(),formalization_id=fid)
    from lea.events import CheckResult
    assert proof_evidence.record_check(chat['id'],'Shared.lean',path,
        CheckResult(str(path),'ok',execution=mock_execution(path),content=path.read_text()),run_id=run['id'],formalization_id=fid)[0]
    store.update_run(run['id'],'proved',result_kind='proved',result_detail='PROVED')
    scope='s:'+owner['id'];core_observer.enroll(scope);finish({**x,'scope':scope})
    assert core.scope_for(session_id=chat['id'])==scope
    assert proofs.get(fid)['validity_status']=='proved'
    assert proofs.current_snapshot(fid)['files'][0]['check_current']
    store.add_code_step(chat['id'],None,'Shared.lean',content='theorem shared : True := by sorry',formalization_id=fid)
    assert proofs.get(fid)['projection']['state']=='inputs_changed'
    assert proofs.get(fid)['certification'] is None
