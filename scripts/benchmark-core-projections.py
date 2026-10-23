#!/usr/bin/env python3
"""Reproducible Stage 2 canary fixture; synthetic evidence, no user DB or paid calls.

Run with the adapter dev venv and redirect stdout to the desired JSON report.
"""
import json,time,statistics,platform,sqlite3,tempfile,os,sys
from pathlib import Path
os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP','True')
ROOT=Path(__file__).resolve().parents[1]
ADAPTER=ROOT/'apps/lea-standalone/adapter'
sys.path[:0]=[str(ADAPTER),str(ADAPTER/'tests')]
import pytest
from test_core_projections import fixture,finish
from app import store,core_observer,db
from app.queries import sessions,proofs
monkey=pytest.MonkeyPatch()
with tempfile.TemporaryDirectory(prefix='lea-stage2-benchmark-') as directory:
 x=fixture.__wrapped__(Path(directory),monkey)
 for i in range(200):
  store.add_code_step(x['session']['id'],None,f'History{i}.lean',content='-- retained historical bytes\n'*100,formalization_id=x['fid'])
 start=time.perf_counter();passes=0
 while True:
  result=core_observer.capture(x['scope']);passes+=1
  if result['state']=='observed':break
 capture_ms=(time.perf_counter()-start)*1000
 report={'fixture':'Synthetic compiler evidence; 201 code rows, one target; isolated temporary database',
         'platform':platform.platform(),'architecture':platform.machine(),'sqlite':sqlite3.sqlite_version,
         'capture_ms':round(capture_ms,3),'capture_batches':passes,'capture_limit':20,'queries':{}}
 for name,fn in [('session_detail',lambda:sessions.session_detail(x['session']['id'])),('session_list',sessions.list_sessions),('proof_snapshot',lambda:proofs.current_snapshot(x['fid']))]:
  values=[]
  for _ in range(30):
   start=time.perf_counter();fn();values.append((time.perf_counter()-start)*1000)
  report['queries'][name]={'samples':len(values),'p50_ms':round(statistics.median(values),3),'p95_ms':round(sorted(values)[int(.95*len(values))-1],3)}
 print(json.dumps(report,indent=2))
monkey.undo()
