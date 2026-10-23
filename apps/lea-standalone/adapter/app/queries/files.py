"""Observed current and historical file content, with explicit synchronization state.

An observation establishes indexed bytes, never a new proof or review verdict.
"""
from datetime import datetime, timezone
from .. import db, observation

# Domain writes that may change materialized project inputs. Observation writes
# themselves do not invalidate a just-published snapshot.
INPUT_TABLES = ('timeline','projects','formalization_files','artifacts','project_files',
                'project_dependencies','project_dependency_attempts','reformalization_applications',
                'project_library_modules','project_library_invocations')


class ProjectionUnavailable(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def input_revision(conn, project_id):
    placeholders = ','.join('?' for _ in INPUT_TABLES)
    return conn.execute(f'''select coalesce(max(id),0) from domain_changes
        where project_id=? and entity_type in ({placeholders})''', (project_id,*INPUT_TABLES)).fetchone()[0]


def enrolled(project_id):
    with db.connect() as conn:
        return bool(conn.execute('select 1 from projection_enrollment where project_id=? and enabled=1',
                                 (project_id,)).fetchone())


def _snapshot(conn, project_id, observation_id=None):
    historical = observation_id is not None
    if historical:
        row = conn.execute("select * from workspace_observations where id=? and project_id=? and state='observed'",
                           (observation_id,project_id)).fetchone()
    else:
        row = conn.execute('''select o.* from projection_enrollment e
            join workspace_observations o on o.id=e.observation_id and o.project_id=e.project_id
            where e.project_id=? and e.enabled=1''',(project_id,)).fetchone()
    if row is None:
        raise ProjectionUnavailable('synchronizing' if not historical else 'observation_not_found')
    if not historical:
        if conn.execute('select 1 from workspace_backfills where project_id=?',(project_id,)).fetchone():
            raise ProjectionUnavailable('synchronizing')
        if row['state']!='observed':
            raise ProjectionUnavailable('synchronizing')
        if datetime.fromisoformat(row['expires_at']) <= datetime.now(timezone.utc):
            raise ProjectionUnavailable('observation_expired')
        # Retention makes old invalidations unavailable: fail closed and rescan.
        floor = conn.execute('select floor from change_retention where id=1').fetchone()[0]
        if row['input_cursor'] < floor or input_revision(conn,project_id)>row['input_cursor']:
            raise ProjectionUnavailable('inputs_changed')
    return dict(row)


def listing(project_id):
    with db.read_snapshot() as conn:
        cursor=observation.watermark(conn)
        snapshot=_snapshot(conn,project_id)
        files=[dict(r) for r in conn.execute('''select path,blob_id,size,state,reason from current_file_bindings
            where project_id=? and observation_id=? order by path''',(project_id,snapshot['id']))]
        return {'version':1,'cursor':cursor,'observation':snapshot,'files':files}


def tree(project_id):
    result=listing(project_id)
    root={}
    for item in result['files']:
        if item['state']=='deleted':
            continue
        parts=item['path'].split('/')
        parent=root
        for i,name in enumerate(parts[:-1]):
            parent=parent.setdefault(name,{'name':name,'path':'/'.join(parts[:i+1]),'type':'dir','children':{}})['children']
        parent[parts[-1]]={'name':parts[-1],'path':item['path'],'type':'file','size':item['size'],
                           'content_state':item['state']}
    def ordered(items):
        return [{**item,**({'children':ordered(item['children'])} if item['type']=='dir' else {})}
                for item in sorted(items.values(),key=lambda i:(i['type']!='dir',i['name'].lower()))]
    return {'tree':ordered(root),'cursor':result['cursor'],'observation':result['observation']}


def content(project_id,path,observation_id=None):
    if not path or path.startswith('/') or any(p in {'','..','.'} for p in path.split('/')):
        raise ProjectionUnavailable('bad_path')
    with db.read_snapshot() as conn:
        cursor=observation.watermark(conn)
        snapshot=_snapshot(conn,project_id,observation_id)
        row=conn.execute('''select f.*,b.content from current_file_bindings f left join artifact_blobs b on b.id=f.blob_id
            where f.project_id=? and f.observation_id=? and f.path=?''',(project_id,snapshot['id'],path)).fetchone()
        if not row or row['state']=='deleted':
            raise ProjectionUnavailable('not_found')
        if row['content'] is None:
            raise ProjectionUnavailable(row['reason'] or 'content_unavailable')
        return {'path':path,'content':row['content'],'lean':path.endswith('.lean'),
                'blob_id':row['blob_id'],'cursor':cursor,'observation':snapshot}
