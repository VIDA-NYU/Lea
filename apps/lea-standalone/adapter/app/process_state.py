"""Public worker status and capabilities are SQL projections, never IPC calls."""
import json
from datetime import datetime,timedelta,timezone
from . import db
LIFETIME_SECONDS=15
BOUNDARY={'version':1,'ui_access':'database','command_direction':'ui_to_worker',
    'processes':'separate','queries':1,'changes':1,'commands':1,'source_publications':1,'editor':1}


def record_start(identity,pid):
    now=datetime.now(timezone.utc)
    with db.write() as conn:
        old=conn.execute('select generation from worker_process where id=1').fetchone()
        conn.execute('''insert into worker_process values(1,?,?,?,'recovering',?,?,?,'{}','{}',null)
            on conflict(id) do update set runtime_id=excluded.runtime_id,generation=excluded.generation,
            pid=excluded.pid,lifecycle_state=excluded.lifecycle_state,started_at=excluded.started_at,
            heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at,capabilities_json='{}',snapshot_json='{}',bridge_hash=null''',
            (identity,old[0]+1 if old else 1,pid,now.isoformat(),now.isoformat(),(now+timedelta(seconds=LIFETIME_SECONDS)).isoformat()))


def publish(identity,value,capabilities):
    now=datetime.now(timezone.utc)
    with db.write() as conn:
        changed=conn.execute('''update worker_process set lifecycle_state=?,heartbeat_at=?,expires_at=?,
            capabilities_json=?,snapshot_json=? where id=1 and runtime_id=?''',
            (value['lifecycle_state'],now.isoformat(),(now+timedelta(seconds=LIFETIME_SECONDS)).isoformat(),
             json.dumps(capabilities),json.dumps(value),identity)).rowcount
    if not changed:raise RuntimeError('Worker process publication was fenced')


def snapshot():
    with db.read_snapshot() as conn:
        row=conn.execute('select * from worker_process where id=1').fetchone()
        queued=conn.execute("select count(*) from runs where status='pending'").fetchone()[0]
    if not row:return {'ready':False,'restart_supported':False,'lifecycle_state':'worker_unavailable','queued_runs':queued}
    live=row['expires_at']>db.utc_now()
    value=json.loads(row['snapshot_json'])
    state=row['lifecycle_state'] if live else 'worker_unavailable'
    boundary=json.loads(row['capabilities_json']).get('database_boundary',{})
    if state=='ready' and any(boundary.get(key)!=expected for key,expected in BOUNDARY.items()):
        state='worker_incompatible'
    return {**value,'runtime_id':row['runtime_id'],'generation':row['generation'],
        'ready':live and state=='ready','restart_supported':live and state in {'ready','draining'},
        'lifecycle_state':state,'queued_runs':queued,'worker_heartbeat_at':row['heartbeat_at'],
        'worker_expires_at':row['expires_at'],'worker_observation':'observed' if live else 'expired'}


def accepting_work():return snapshot()['ready']


def health():
    from .release_info import identity
    with db.read_snapshot() as conn:
        row=conn.execute('select capabilities_json from worker_process where id=1').fetchone()
        value=snapshot()
    caps=json.loads(row[0]) if row else {}
    if not value['ready']:
        for kind in ('commands','reformalization','lea_status'):
            if kind in caps:caps[kind]={**caps[kind],'admission_enabled':False}
    return {'ok':True,**value,'release':identity(),'capabilities':{**caps,'database_boundary':BOUNDARY}}


def capability():
    return health()['capabilities'].get('execution',{'available':False,'reason':'Worker execution capability is unavailable.'})
