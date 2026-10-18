"""Pure SQL source identities and frozen business bindings; no materializer."""
import json
from .. import db

def receipt(row):
    return {'version':1,'source_context_ref':row['id'],'captured_at':row['created_at'],
            'source_archive_ref':json.loads(row['files_json']).get('$archive'),
            'source_identity_hash':row['source_identity_hash'],'source_bundle_hash':row['bundle_hash'],
            'coverage':row['coverage'],'generation':row['generation'],'sequence':row['sequence']}


def _validate_binding(conn,form,document,source):
    binding=conn.execute('select * from overleaf_bindings where formalization_id=?',(form['id'],)).fetchone()
    expected=f"{document}:{source['targetKind']}:{source['targetKey']}"
    if form.get('origin')!='overleaf' or (binding and (binding['document_id'],binding['target_kind'],binding['target_key'])!=(document,source['targetKind'],source['targetKey'])) or (not binding and form.get('origin_key')!=expected):
        raise ValueError('Published source belongs to a different document or target')


def resolve(conn,reference,formalization_id=None,*,admission=True):
    row=conn.execute('select * from source_publications where id=?',(reference,)).fetchone()
    if not row:raise ValueError('Published source is unavailable. Publish this target from Overleaf before sending.')
    source=json.loads(row['bundle_json'])
    head=conn.execute('select publication_id from source_heads where document_id=? and target_kind=? and target_key=?',
        (row['document_id'],source['targetKind'],source['targetKey'])).fetchone()
    latest=conn.execute('select * from source_publications where id=?',(head[0],)).fetchone() if head else None
    if not latest or latest['bundle_hash']!=row['bundle_hash'] or latest['coverage']!=row['coverage'] or latest['files_json']!=row['files_json']:
        raise ValueError('Published source changed. Select its current publication and submit again.')
    document_head=conn.execute('select * from source_publications where document_id=? and generation=? order by sequence desc limit 1',(row['document_id'],row['generation'])).fetchone()
    if document_head and document_head['coverage']=='complete' and row['coverage']=='complete' and document_head['files_json']!=row['files_json']:
        raise ValueError('The published document archive changed. Publish this target from its current archive before sending.')
    owner=conn.execute('select * from source_publishers where document_id=?',(row['document_id'],)).fetchone()
    if not owner or owner['generation']!=row['generation'] or owner['client_id']!=row['client_id']:
        raise ValueError('The source publisher generation changed. Publish from the current tab before sending.')
    parent=db.caller_parent_command.get()
    bound=conn.execute("select 1 from companion_source_bindings b join commands c on c.id=b.command_id where b.command_id=? and b.publication_id=? and c.kind='companion.dispatch' and c.state in ('claimed','succeeded')",(parent,reference)).fetchone() if parent else None
    if parent:
        caller=conn.execute('select kind from commands where id=?',(parent,)).fetchone()
        if caller and caller[0]=='companion.dispatch' and not bound:raise ValueError('This source was not frozen by the accepted business operation')
    if admission and not bound and owner['lease_until']<=db.utc_now():raise ValueError('Published source is stale. Publish it again from Overleaf before sending.')
    if formalization_id:
        form=conn.execute('select * from formalizations where id=?',(formalization_id,)).fetchone()
        if not form:raise ValueError('Formalization not found')
        _validate_binding(conn,dict(form),row['document_id'],source)
        if form['source_hash'] and form['source_hash']!=source['sourceIdentityHash']:raise ValueError('The selected formalization source changed outside this publication')
    return source


def latest(formalization_id):
    with db.read_snapshot() as conn:
        binding=conn.execute('select * from overleaf_bindings where formalization_id=?',(formalization_id,)).fetchone()
        parent=db.caller_parent_command.get()
        if parent and binding:
            pinned=conn.execute('''select p.* from companion_source_bindings b join source_publications p on p.id=b.publication_id
                where b.command_id=? and p.document_id=? and json_extract(p.bundle_json,'$.targetKind')=? and json_extract(p.bundle_json,'$.targetKey')=?''',
                (parent,binding['document_id'],binding['target_kind'],binding['target_key'])).fetchone()
            if pinned:
                resolve(conn,pinned['id'],formalization_id)
                return receipt(pinned)
        row=conn.execute('select p.* from source_heads h join source_publications p on p.id=h.publication_id where h.formalization_id=?',
            (formalization_id,)).fetchone()
        if not row and binding:
            row=conn.execute('select p.* from source_heads h join source_publications p on p.id=h.publication_id where h.document_id=? and h.target_kind=? and h.target_key=?',
                (binding['document_id'],binding['target_kind'],binding['target_key'])).fetchone()
        if not row:raise ValueError('No published source exists for this target. Publish it from Overleaf before sending.')
        resolve(conn,row['id'],formalization_id)
        return receipt(row)

