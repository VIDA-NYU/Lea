"""Native real-compiler evidence crosses the worker/SQL query boundary."""
from pathlib import Path
import shutil
import pytest
from app import db,store,core_observer,proof_evidence
from app.config import LeaConfig
from app.queries import proofs
from lea import interface,lsp_daemon,execution


def test_real_transitive_compiler_evidence_is_observed_without_query_execution(tmp_path,monkeypatch):
    if not shutil.which('lake') or not execution.capability()['available']:
        pytest.skip('Native Lean and confined executor are required')
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'data/db.sqlite');db.init_db()
    root=tmp_path/'prover';workspace=root/'workspace';repo=workspace/'proofs/Lea/Real';repo.mkdir(parents=True)
    (workspace/'lakefile.toml').write_text('name="core_projection"\n[[lean_lib]]\nname="Lea"\nsrcDir="proofs"\n')
    shutil.copyfile(Path(__file__).parents[2]/'prover/workspace/lean-toolchain',workspace/'lean-toolchain')
    base=repo/'Base.lean';base.write_text('namespace Lea.Real\ntheorem base : True := by trivial\nend Lea.Real\n')
    path=repo/'Proof.lean';path.write_text('import Lea.Real.Base\nnamespace Lea.Real\ntheorem result : True := base\nend Lea.Real\n')
    monkeypatch.setattr('app.config.load_config',lambda:LeaConfig(model='fixture',max_turns=3,lea_root=root))
    project=store.create_project('real',namespace='Lea.Real');session=store.create_session('Real',project_id=project['id'])
    form=store.create_formalization(project_id=project['id'],loose_session_id=None,display_title='Real result',declaration_name='Lea.Real.result')
    store.link_session_formalization(session['id'],form['id']);store.link_formalization_file(form['id'],'Proof.lean','primary')
    run=store.create_run(session['id'],'fixture',None,3,focus_formalization_id=form['id'])
    try:
        checked=interface.check(str(path));assert checked.status=='ok',checked.detail
        store.add_code_step(session['id'],run['id'],'Proof.lean',content=path.read_text(),formalization_id=form['id'])
        assert proof_evidence.record_check(session['id'],'Proof.lean',path,checked,run_id=run['id'],formalization_id=form['id'])[0]
        store.update_run(run['id'],'proved',result_kind='proved',result_detail='PROVED')
        scope='p:'+project['id'];core_observer.enroll(scope)
        def denied(*a,**kw):raise AssertionError('Observation or GET started a compiler')
        monkeypatch.setattr(interface,'check',denied)
        monkeypatch.setattr('subprocess.Popen',denied)
        assert core_observer.capture(scope)['state']=='observed'
        assert proofs.get(form['id'])['validity_status']=='proved'
        base.write_text(base.read_text().replace('theorem base','theorem renamed'))
        assert core_observer.capture(scope)['state']=='observed'
        assert not proofs.get(form['id'])['check_current']
        assert proofs.current_snapshot(form['id'])['files'][0]['check_status']=='ok'
    finally:lsp_daemon._shutdown_all()
