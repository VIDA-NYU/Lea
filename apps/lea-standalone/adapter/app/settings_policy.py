"""Database application policy; credential/bootstrap values stay in infrastructure."""
import hashlib
import json
from . import db

FIELDS={'model','max_turns','max_spend_usd','permission_tier','narrate_tool_steps'}


def read():
    if not db.DB_PATH.exists():return None
    with db.connect() as conn:
        if not conn.execute("select 1 from sqlite_master where type='table' and name='application_settings'").fetchone():return None
        row=conn.execute("select payload from application_settings where id='policy'").fetchone()
    return json.loads(row[0]) if row else None


def bootstrap():
    if read() is None:
        from .config import ROOT,read_config_data
        from pathlib import Path
        import os
        source=read_config_data(Path(os.environ.get('LEA_CONFIG_FILE',ROOT/'config/lea.local.toml')))
        value={k:v for k,v in source.items() if k in FIELDS}
        encoded=json.dumps(value,sort_keys=True)
        with db.write() as conn:
            conn.execute('insert or ignore into application_settings values(?,?,?,?)',
                ('policy',encoded,json.dumps({'version':1,'source':'legacy_non_secret_toml','sha256':hashlib.sha256(encoded.encode()).hexdigest()}),db.utc_now()))
    with db.connect() as conn:
        if conn.execute("select 1 from application_settings where id='subagent_overrides'").fetchone():return
    from . import subagent_overrides
    overrides,error=subagent_overrides.load_overrides_checked()
    with db.write() as conn:
        conn.execute('insert or ignore into application_settings values(?,?,?,?)',
            ('subagent_overrides',json.dumps(overrides),json.dumps({'version':1,'source':'legacy_overrides','error':error}),db.utc_now()))


def update(updates):
    with db.write() as conn:
        from .workspace_commands import authorize
        authorize(conn)
        row=conn.execute("select payload from application_settings where id='policy'").fetchone()
        value=json.loads(row[0]) if row else {}
        value.update({k:v for k,v in updates.items() if k in FIELDS})
        conn.execute('insert into application_settings values(?,?,?,?) on conflict(id) do update set payload=excluded.payload,provenance=excluded.provenance,updated_at=excluded.updated_at',
            ('policy',json.dumps(value),'{"source":"user_command","version":1}',db.utc_now()))


def stage_credentials(values):
    """Explicit infrastructure intake: no credential enters a command or SQL blob."""
    import os
    from uuid import uuid4
    identity=uuid4().hex
    path=db.DB_PATH.parent/'credential-intake'/identity
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=json.dumps(values).encode()
    if len(raw)>65536:raise ValueError('Credential/settings intake exceeds 64 KiB')
    with open(path,'xb') as target:
        os.chmod(path,0o600);target.write(raw);target.flush();os.fsync(target.fileno())
    return identity


def consume(ticket):
    if len(ticket)!=32 or any(c not in '0123456789abcdef' for c in ticket):raise ValueError('Invalid credential reference')
    path=db.DB_PATH.parent/'credential-intake'/ticket
    with path.open('rb') as source:raw=source.read(65537)
    if len(raw)>65536:raise ValueError('Credential intake exceeds its bound')
    return json.loads(raw),path
