#!/usr/bin/env python3
"""Reproducible synthetic runtime/storage baseline; never opens the user's DB.

Use the adapter venv interpreter. JSON reports identify workload, runtime,
journal mode and limitations. No model/provider calls or existing project data.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
sys.path[:0] = [str(ROOT / "apps/lea-standalone/adapter"), str(ROOT / "apps/lea-standalone/prover")]
from app import db, store, config


def summary(values):
    values = sorted(values)
    return {"samples": len(values), "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(values[min(len(values)-1, int(len(values)*.95))], 3),
            "max_ms": round(max(values), 3)}


def timed(fn, count=24):
    values = []
    for _ in range(count):
        start = time.perf_counter(); fn(); values.append((time.perf_counter()-start)*1000)
    return summary(values)


def run(mode, base):
    db.DB_PATH = base / mode / "data/lea-interface.sqlite3"
    config.ROOT = base / mode
    db.init_db()
    with db.connect() as conn:
        actual_mode = conn.execute(f"pragma journal_mode={mode}").fetchone()[0]
    result = {"requested_journal": mode, "actual_journal": actual_mode, "projects": []}
    for name, files, revisions, size in [("small", 16, 3, 2048), ("medium", 128, 3, 8192), ("large", 512, 3, 8192)]:
        project = store.create_project(name)
        sessions = []
        for i in range(files):
            session = store.create_session(f"Target {i}", project_id=project["id"])
            sessions.append(session["id"])
            for revision in range(revisions):
                header = f"-- fixture {name} {i} revision {revision}\n"
                code = header + "x" * (size-len(header))
                store.add_code_step(session["id"], None, f"Target{i}.lean", content=code)
        sid = sessions[-1]
        result["projects"].append({"name": name, "files": files, "revisions_per_file": revisions,
            "content_bytes": files*revisions*size,
            "session_detail": timed(lambda: store.session_detail(sid)),
            "session_list": timed(store.list_sessions, 8)})
        print(f"Measured {mode} {name}", file=sys.stderr, flush=True)

    session = store.create_session("Four concurrent progress writers")
    latencies = []
    def writer(index):
        observed = []
        for n in range(32):
            start = time.perf_counter()
            store.add_message(session["id"], "assistant", f"{index}:{n}:" + "t"*16384)
            observed.append((time.perf_counter()-start)*1000)
        return observed
    with db.connect() as pin:
        pin.execute("select count(*) from timeline").fetchone()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for values in pool.map(writer, range(4)): latencies.extend(values)
        result["four_writer_16k_progress"] = summary(latencies)
        result["database_bytes"] = db.DB_PATH.stat().st_size
        wal = Path(str(db.DB_PATH) + "-wal")
        result["wal_bytes_before_checkpoint"] = wal.stat().st_size if wal.exists() else 0
        result["integrity"] = pin.execute("pragma quick_check").fetchone()[0]
        result["wal_checkpoint"] = list(pin.execute("pragma wal_checkpoint(passive)").fetchone())
        result["wal_bytes_after_passive_checkpoint"] = wal.stat().st_size if wal.exists() else 0

    # Model the proposed bounded payload backlog, not a solver throughput claim.
    with db.write() as conn:
        conn.execute("create table benchmark_commands (id integer primary key,payload text,state text)")
    for count in (32, 128, 256):
        with db.write() as conn: conn.execute("delete from benchmark_commands")
        values = []
        for i in range(count):
            start = time.perf_counter()
            with db.write() as conn:
                conn.execute("insert into benchmark_commands values (?,?,'queued')", (i, "q"*65536))
            values.append((time.perf_counter()-start)*1000)
        result.setdefault("queue_payload_sweep", []).append({"queued": count,
            "payload_bytes": count*65536, "durable_insert": summary(values)})
    for size in (4*1024**2, 25*1024**2, 64*1024**2):
        payload = "b"*size
        start = time.perf_counter()
        with db.write() as conn: blob = store._put_blob(conn, payload)
        insert_ms = (time.perf_counter()-start)*1000
        result.setdefault("blob_sweep", []).append({"bytes": size, "insert_ms": round(insert_ms, 3),
            "read": timed(lambda: store.blob_content(blob), 3),
            "sha256": hashlib.sha256(payload.encode()).hexdigest()})
    return result


def http_baseline(base):
    prover = base / "prover"
    (prover / "workspace/proofs").mkdir(parents=True)
    (prover / "workspace/projects").mkdir()
    (prover / "lea").mkdir()
    (prover / "pyproject.toml").write_text('[project]\nname="baseline-fixture"\n')
    (prover / "workspace/lean-toolchain").write_text('leanprover/lean4:v4.29.0\n')
    (prover / "workspace/lakefile.lean").write_text('-- fixture\n')
    (base / "config").mkdir()
    (base / "config/lea.local.toml").write_text(f'model="o4-mini"\nmax_turns=5\nlea_root={json.dumps(str(prover))}\n')
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not any(part in key.upper() for part in ("API_KEY", "TOKEN", "PASSWORD"))}
    env.update(LEA_ITEST_HOME=str(base), LEA_ITEST_PORT=str(port), LEA_MODEL_CATALOG_OFFLINE="true",
               PYTHONPATH=os.pathsep.join([str(ROOT / "apps/lea-standalone/prover"), str(ROOT / "apps/lea-standalone/adapter")]))
    def request(route, body=None):
        req = Request(f"http://127.0.0.1:{port}"+route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=20) as response: return json.load(response)
    with tempfile.TemporaryFile(mode="w+") as log:
        child = subprocess.Popen([sys.executable, str(ROOT / "tests/integration/adapter_entry.py")], env=env, stdout=log, stderr=log)
        try:
            start = time.perf_counter()
            for _ in range(240):
                if child.poll() is not None:
                    log.seek(0); raise RuntimeError(log.read()[-4000:])
                try:
                    if request('/api/ready')['ready']: break
                except OSError: pass
                time.sleep(.05)
            else: raise RuntimeError('Fixture readiness timed out')
            result = {"startup_ms": round((time.perf_counter()-start)*1000,3),
                      "health": request('/api/health'), "health_read": timed(lambda: request('/api/health'))}
            controls = []
            for i in range(8):
                run = request('/api/runs', {'message': f'Formalize the Overleaf theorem labeled fixture_{i}. [stub:approvals]'})
                route = f"/api/sessions/{run['session_id']}"
                for _ in range(200):
                    detail = request(route)
                    if (detail.get('active_run') or {}).get('pending_approval'): break
                    time.sleep(.01)
                else:
                    log.flush(); log.seek(0)
                    raise RuntimeError(f'Approval fixture never parked: {json.dumps(detail)}\n{log.read()[-8000:]}')
                if i == 0:
                    with urlopen(f"http://127.0.0.1:{port}/api/runs/{run['run_id']}/events", timeout=10) as stream:
                        first = stream.readline().decode().strip()
                    result['disconnected_observer_does_not_cancel'] = request(f"/api/runs/{run['run_id']}")['status'] == 'running'
                    with urlopen(f"http://127.0.0.1:{port}/api/runs/{run['run_id']}/events", timeout=10) as stream:
                        result['reconnect_first_frame_matches'] = first == stream.readline().decode().strip()
                    result['parked_session_detail'] = timed(lambda: request(route))
                start = time.perf_counter()
                request(f"/api/runs/{run['run_id']}/interrupt", {})
                controls.append((time.perf_counter()-start)*1000)
                for _ in range(200):
                    if request(f"/api/runs/{run['run_id']}")['status'] not in ('pending','running'): break
                    time.sleep(.01)
            result['cancellation_acceptance'] = summary(controls)
            result['session_list'] = timed(lambda: request('/api/sessions'))
            if result['health']['capabilities']['commands'].get('workspace_version'):
                session_id=run['session_id'];path='WorkspaceEdit.lean'
                accepted,completed,check_accepted,check_completed=[],[],[],[]
                def settle(receipt):
                    for _ in range(600):
                        value=request('/api/workspace/commands/'+receipt['command']['id'])['command']
                        if value['state'] in ('succeeded','failed','suspended','expired','cancelled'):
                            if value['state']!='succeeded':raise RuntimeError('Workspace fixture failed: '+json.dumps(value))
                            return value['result']
                        time.sleep(.05)
                    raise RuntimeError('Workspace receipt did not settle')
                failed_verdicts=0
                for i in range(8):
                    current=request(f'/api/workspace/file?session_id={session_id}&path={path}')
                    content='theorem workspace_fixture : True := by '+('bad_tactic' if i==7 else 'trivial')+'\n-- revision '+str(i)+'\n'
                    start=time.perf_counter()
                    receipt=request('/api/workspace/commands',{'version':1,'action':'session.save',
                        'idempotency_key':'baseline-save-'+str(i),'draft_id':'draft-'+str(i),
                        'expected_revision':current['file_revision'],
                        'arguments':{'session_id':session_id,'request':{'path':path,'content':content}}})
                    accepted.append((time.perf_counter()-start)*1000)
                    saved=settle(receipt);completed.append((time.perf_counter()-start)*1000)
                    start=time.perf_counter()
                    receipt=request('/api/workspace/commands',{'version':1,'action':'session.check',
                        'idempotency_key':'baseline-check-'+str(i),'expected_revision':saved['file_revision'],
                        'arguments':{'session_id':session_id,'request':{'path':path}}})
                    check_accepted.append((time.perf_counter()-start)*1000)
                    checked=settle(receipt);check_completed.append((time.perf_counter()-start)*1000)
                    failed_verdicts+=checked['status']=='error'
                result['workspace']={'save_acceptance':summary(accepted),'save_observed_completion':summary(completed),
                    'check_acceptance':summary(check_accepted),'check_observed_completion':summary(check_completed),
                    'failed_lean_verdict_successful_commands':failed_verdicts,
                    'final_sql_content_matches':request(f'/api/workspace/file?session_id={session_id}&path={path}')['content']==content,
                    'compiler':'scripted exact-byte check; no native Lean or paid provider'}
            if result['health']['capabilities']['commands'].get('source_publication_version'):
                from app.source_context import source_hash
                value={'version':2,'targetKey':'baseline','targetKind':'theorem','statement':'True.', 'proof':'',
                    'proofAssociation':{'status':'missing','method':'none','sourceFile':'main.tex','proofHash':''},
                    'statementLocation':{'sourceFile':'main.tex'},'uses':[],'context':'','relevantSource':[],'mirror':None}
                value['bundleHash']=source_hash(value)
                authority=request('/api/overleaf/publishers',{'document_id':'baseline','client_id':'baseline'})
                publications=[];archive=None
                for i in range(8):
                    body={'version':1,'document_id':'baseline','client_id':'baseline','generation':authority['generation'],
                        'sequence':i+1,'source_bundle':{**value,'targetKey':'baseline'},'coverage':'partial'}
                    if archive:body['archive_ref']=archive
                    else:body['files']={'main.tex':'x'*1024*1024}
                    start=time.perf_counter();published=request('/api/overleaf/publications',body)
                    publications.append((time.perf_counter()-start)*1000);archive=published['source_archive_ref']
                queries=[];observed=[]
                for i in range(8):
                    start=time.perf_counter()
                    accepted=request('/api/companion/commands',{'path':'/statuses','method':'POST','idempotency_key':'baseline-query-'+str(i),
                        'payload':{'overleafProjectId':'baseline','targets':[]}})
                    queries.append((time.perf_counter()-start)*1000)
                    for _ in range(600):
                        receipt=request('/api/companion/commands/'+accepted['command']['id'])['command']
                        if receipt['state']=='succeeded':break
                        if receipt['state'] in ('failed','expired','suspended'):raise RuntimeError('Compatibility observation failed: '+json.dumps(receipt))
                        time.sleep(.05)
                    else:raise RuntimeError('Compatibility observation timed out')
                    observed.append((time.perf_counter()-start)*1000)
                result['stage6']={'source_publication':summary(publications),'source_bytes':1024*1024,
                    'unchanged_archive_reused':True,'compatibility_query_acceptance':summary(queries),
                    'compatibility_query_observed_completion':summary(observed),'worker_runtime':'adapter-owned Node SQLite; no paid model'}
            return result
        finally:
            child.terminate()
            try: child.wait(timeout=10)
            except subprocess.TimeoutExpired: child.kill(); child.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--http-only", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lea-db-baseline-") as directory:
        base = Path(directory)
        report = {"version": 1, "captured_at": datetime.now(timezone.utc).isoformat(),
            "workload": "synthetic history and SQLite operations; no paid solver or compiler",
            "environment": {"system": platform.system(), "machine": platform.machine(),
                "release": platform.release(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
            "measurements": [] if args.http_only else [run(mode, base) for mode in ("delete", "wal")]}
        if args.http_only: report['http'] = http_baseline(base)
        from lea.execution import capability
        report["executor_capability"] = capability()
        report["source_sha256"] = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("scripts/benchmark-db-boundary.py", "apps/lea-standalone/adapter/app/db.py",
                         "apps/lea-standalone/adapter/app/store.py", "apps/lea-standalone/adapter/app/bridge.py",
                         "apps/lea-standalone/adapter/app/commands.py","apps/lea-standalone/adapter/app/command_host.py",
                         "apps/lea-standalone/adapter/app/run_commands.py","apps/lea-standalone/adapter/app/command_execution.py",
                         "apps/lea-standalone/adapter/app/main.py","apps/lea-standalone/adapter/app/workspace_commands.py",
                         "apps/lea-standalone/adapter/app/workspace_files.py","apps/lea-standalone/adapter/app/workspace_effects.py",
                         "apps/lea-standalone/adapter/app/workspace_edits.py","apps/lea-standalone/adapter/app/routes/workspace.py",
                         "apps/lea-standalone/adapter/app/routes/projects.py","apps/lea-standalone/adapter/app/routes/sessions.py",
                         "apps/lea-standalone/adapter/app/projects.py","apps/lea-standalone/adapter/app/uploads.py",
                         "apps/lea-standalone/adapter/app/proof_files.py","apps/lea-standalone/adapter/app/proof_evidence.py",
                         "apps/lea-standalone/adapter/app/project_dependencies.py","apps/lea-standalone/adapter/app/reformalization_apply.py",
                         "apps/lea-standalone/adapter/app/formalization_reviews.py","apps/lea-standalone/adapter/app/reformalization_review.py",
                         "apps/lea-standalone/adapter/migrations/versions/0043_workspace_commands.py",
                         "apps/lea-standalone/adapter/app/source_publications.py","apps/lea-standalone/adapter/app/companion_service.py",
                         "apps/lea-standalone/adapter/app/companion_import.py","apps/lea-standalone/adapter/app/peripheral_views.py",
                         "apps/lea-standalone/adapter/app/settings_policy.py","apps/lea-standalone/adapter/app/routes/companion.py",
                         "apps/lea-standalone/adapter/app/routes/overleaf_source.py","apps/lea-standalone/adapter/app/routes/runs.py",
                         "apps/lea-standalone/adapter/migrations/versions/0044_source_companion_projections.py",
                         "apps/lea-standalone/adapter/migrations/versions/0045_source_archive_references.py",
                         "apps/lea-standalone/adapter/migrations/versions/0046_project_namespace_generation.py",
                         "apps/lea-standalone/adapter/app/queries/identity.py","apps/lea-standalone/adapter/app/queries/sources.py",
                         "apps/overleaf-extension/companion/server.mjs","apps/overleaf-extension/companion/worker.mjs",
                         "apps/overleaf-extension/companion/workerService.mjs","apps/overleaf-extension/companion/databaseState.mjs",
                         "apps/overleaf-extension/extension/sourcePublisher.mjs","apps/overleaf-extension/extension/commandClient.mjs",
                         "apps/overleaf-extension/companion/leaApiClient.mjs","apps/overleaf-extension/extension/content.js",
                         "apps/lea-standalone/adapter/app/core_query_dispatch.py","apps/lea-standalone/adapter/app/core_observer.py","apps/lea-standalone/adapter/app/queries/core.py",
                         "apps/lea-standalone/adapter/app/queries/proofs.py","apps/lea-standalone/adapter/app/queries/sessions.py")}
        report["peak_rss_platform_units"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["limitations"] = ["Synthetic fixtures do not measure provider/Lean latency or production project throughput.",
            "Queue storage sweeps do not measure provider or solver service throughput.",
            "WAL trial changes only the temporary database; current application journal policy is unchanged."]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+"\n")
    print(str(args.output))


if __name__ == "__main__": main()
