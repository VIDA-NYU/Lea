"""Project identity and namespace versions from one SQL snapshot."""
from .. import db


def project_identity(project, *, overleaf_project_id=None):
    with db.read_snapshot() as conn:
        row=conn.execute('select * from projects where id=?',(project['id'],)).fetchone()
        if not row:raise LookupError('Project not found')
        project=dict(row)
        recorded=conn.execute("""select exists(select 1 from timeline t join sessions s on s.id=t.session_id
            where s.project_id=? and t.kind='code' and lower(t.path) not like '%scratch%')""",(project['id'],)).fetchone()[0]
        changing=conn.execute("""select exists(select 1 from commands c join workspace_requests r on r.command_id=c.id
            where c.project_id=? and r.action='project.identity' and (c.state='claimed' or exists(
                select 1 from workspace_effects e where e.command_id=c.id and e.state in ('prepared','installed','conflict'))))""",(project['id'],)).fetchone()[0]
        return {'namespaceRevision':project['namespace_generation'],'renameInProgress':bool(changing),
            'projectId':project['id'],'overleafProjectId':overleaf_project_id or project['slug'],
            'slug':project['slug'],'projectName':project['title'],'nameSource':project['name_source'],
            'namespace':project['namespace'],'namespaceEditable':True,'repoPath':project.get('repo_path'),
            'hasRecordedProofs':bool(recorded),'exists':True}
