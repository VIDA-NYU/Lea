"""Real Lean checks with SQL stop gates and the real observers in a disposable stack.

No provider calls or production database/workspace writes. Shared Lake packages
are read-only under the same macOS confinement as profile-lean-checks.py.
"""
import argparse
import cProfile
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pstats
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def measure(root, include_analysis):
    from app import config, db, store, commands, run_commands, core_observer, workspace_observer, proof_evidence, bridge
    from lea import check_evidence, lsp_daemon, tools
    from lea.check_runtime import CheckBudget
    from lea.lean_checks import check_file
    from lea.events import CheckResult
    from lea.runctx import run_context
    base = root.parent.parent
    os.environ['LEA_OWNED_PROCESS_DIR'] = str(base / 'owned-processes')
    config.ROOT = base / 'application'
    (config.ROOT / 'config').mkdir(parents=True)
    config.load_config = lambda:config.LeaConfig(model='offline', max_turns=1, lea_root=root.parent)
    db.DB_PATH = base / 'data/profile.sqlite3'
    db.init_db()
    project = store.create_project('profile', namespace='Lea.Profile')
    session = store.create_session('Profile', project_id=project['id'])
    form = store.create_formalization(project_id=project['id'], loose_session_id=None,
        display_title='Square', declaration_name='square')
    store.link_session_formalization(session['id'], form['id'])
    store.link_formalization_file(form['id'], 'Main.lean', 'primary')
    run = store.create_run(session['id'], 'offline', None, 1, focus_formalization_id=form['id'])
    store.claim_pending_run(run['id'])
    with db.write() as conn:run_commands.attach_run(conn, store.get_run(run['id']), already_accepted=True)
    epoch = commands.start_worker('profile')
    token = commands.claim('profile', epoch, 'compute', kinds={'run.execute'})
    scope = 'p:' + project['id']
    core_observer.enroll(scope)
    path = root / 'proofs/Lea/Profile/Main.lean'
    path.parent.mkdir(parents=True)
    code = ('import Mathlib.Data.Real.Basic\nimport Mathlib.Tactic.Ring.RingNF\n'
        'namespace Lea.Profile\ntheorem square (x y : ℝ) :\n'
        ' (x+y)^2+(x-y)^2 = 2*x^2+2*y^2 := by ring\nend Lea.Profile\n')
    stop = threading.Event()
    scans, errors, cases = [], [], []
    fingerprints = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        ROOT/'apps/lea-standalone/adapter/app/bridge.py', ROOT/'apps/lea-standalone/adapter/app/core_observer.py',
        Path(check_evidence.__file__), ROOT/'apps/lea-standalone/prover/lea/check_runtime.py', Path(tools.__file__), Path(__file__)]}
    def observe():
        while not stop.is_set():
            start = time.monotonic()
            try:
                commands.heartbeat_execution('profile', epoch)
                workspace_observer.maintain()
                result = core_observer.maintain()
                scans.append({'seconds':round(time.monotonic()-start, 3), 'state':result.get('state')})
            except Exception as exc:errors.append(type(exc).__name__ + ': ' + str(exc))
            stop.wait(1)
    def should_stop():
        # Mirrors the original worker's two SQL reads. No paid effects follow
        # this probe; ownership is authoritative throughout the measurement.
        if hasattr(bridge, '_run_stop_status'):
            current, owned = bridge._run_stop_status(run['id'], token)
        else:
            current = store.get_run_status(run['id'])
            with db.connect() as conn:owned = commands._owned(conn, token)
        return stop.is_set() or not current or not owned or bool(current.get('stop_requested_reason'))
    thread = threading.Thread(target=observe, name='profile-observer', daemon=True)
    thread.start()
    try:
        for name, content in [('cold',code), ('proof_edit',code.replace('by ring','by\n  ring')), ('warm',code.replace('by ring','by\n  ring'))]:
            path.write_text(content)
            if name != 'warm':
                store.add_code_step(session['id'], run['id'], 'Main.lean', content=content, formalization_id=form['id'])
            stats = cProfile.Profile();stats.enable()
            checked = check_file(path, str(root), budget=CheckBudget(timeout=300, should_stop=should_stop))
            stats.disable()
            assert checked.status == 'ok', checked.output
            recorded = CheckResult(str(path), checked.status, checked.output,
                execution=checked.execution, content=checked.captured_content)
            assert proof_evidence.record_check(session['id'], 'Main.lean', path, recorded,
                run_id=run['id'], formalization_id=form['id'])[0]
            top = sorted(pstats.Stats(stats).stats.items(), key=lambda item:item[1][3], reverse=True)[:20]
            case = {'case':name,'execution':check_evidence.public_execution(checked.execution),
                'environment_files':len(checked.execution.get('environment',{}).get('files',{})),
                'profile':[{'function':k[2], 'file':Path(k[0]).name, 'calls':v[1],
                    'self_seconds':round(v[2],4), 'cumulative_seconds':round(v[3],4)} for k,v in top]}
            cases.append(case);print(json.dumps(case),flush=True)
        if include_analysis:
            generation=checked.execution['daemon_generation']
            start = time.monotonic()
            with run_context(working_dir=str(path.parent), should_stop=should_stop):result = tools.suggest_imports(str(path))
            assert path.read_text() == content
            case = {'case':'advisory_import_analysis','seconds':round(time.monotonic()-start,3),'result':str(result)}
            cases.append(case);print(json.dumps(case),flush=True)
            checked=check_file(path,str(root),budget=CheckBudget(timeout=60,should_stop=should_stop))
            assert checked.status=='ok' and checked.execution['daemon_generation']==generation, checked.output
            case={'case':'post_advice_warm','execution':check_evidence.public_execution(checked.execution)}
            cases.append(case);print(json.dumps(case),flush=True)
    finally:
        stop.set();thread.join(timeout=30);lsp_daemon._shutdown_all()
    assert not thread.is_alive(), 'Observer did not stop'
    assert not errors, errors
    return {'version':1,'captured_at':datetime.now(timezone.utc).isoformat(),'source_sha256':fingerprints,
        'isolation':'Disposable SQL/workspace; real core/workspace observers and SQL ownership reads; macOS confinement; packages read-only; no network/providers',
        'cases':cases,'observer_scans':scans,'observer_errors':errors}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--include-analysis',action='store_true')
    parser.add_argument('--prepared',type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.prepared:
        args.output.write_text(json.dumps(measure(args.prepared,args.include_analysis),indent=2)+'\n');return
    from lea import execution
    assert execution.capability()['available'], 'Supported confined executor required'
    with tempfile.TemporaryDirectory(prefix='lea-live-profile-') as temp:
        base=Path(temp).resolve();root=base/'prover/workspace';root.mkdir(parents=True)
        workspace=ROOT/'apps/lea-standalone/prover/workspace'
        for name in ['lean-toolchain','lakefile.lean','lake-manifest.json']:shutil.copyfile(workspace/name,root/name)
        packages=root/'.lake/packages';packages.mkdir(parents=True)
        for package in (workspace/'.lake/packages').iterdir():(packages/package.name).symlink_to(package.resolve(),target_is_directory=True)
        (base/'tmp').mkdir()
        env={**os.environ,'TMPDIR':str(base/'tmp'),'PYTHONDONTWRITEBYTECODE':'1','LEA_PROCESS_ROLE':'test',
            'LEA_MODEL_CATALOG_OFFLINE':'true','LITELLM_LOCAL_MODEL_COST_MAP':'True'}
        output=base/'result.json'
        subprocess.run(['sandbox-exec','-p',execution.profile({'writable_roots':[str(base)]}),sys.executable,
            str(Path(__file__).resolve()),'--prepared',str(root),'--output',str(output),
            *(['--include-analysis'] if args.include_analysis else [])],env=env,check=True,timeout=1200)
        args.output.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(output,args.output)
        print(json.dumps({'saved':str(args.output)}))


if __name__=='__main__':main()
