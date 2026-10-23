"""Accepted SQL input outlives the browser publisher, in both admission tiers."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

from app import commands, companion_service, db, source_publications as source, store, run_commands
from app.source_context import source_hash


DOCUMENT = 'accepted-source-document'
OWNER = 'source-lifetime-worker'
WORKER_MODULE = Path(__file__).resolve().parents[3] / 'overleaf-extension/companion/databaseState.mjs'


def bundle(key='target', statement='Statement'):
    value = {'version': 2, 'targetKey': key, 'targetKind': 'theorem',
             'statement': statement, 'proof': 'Proof',
             'proofAssociation': {'status': 'associated', 'method': 'adjacent',
                                  'sourceFile': 'main.tex', 'proofHash': hashlib.sha256(b'Proof').hexdigest()},
             'statementLocation': {'sourceFile': 'main.tex'}, 'uses': [],
             'context': '', 'relevantSource': [], 'mirror': None}
    value['bundleHash'] = source_hash(value)
    return value


def publish(*, client='tab', generation=1, sequence=1, key='target',
            statement='Statement', text='immutable editor bytes', coverage='complete'):
    return source.publish(source.PublicationRequest(
        document_id=DOCUMENT, client_id=client, generation=generation, sequence=sequence,
        source_bundle=bundle(key, statement), files={'main.tex': text}, coverage=coverage))


@pytest.fixture
def accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'test.sqlite3')
    db.init_db()
    source.publisher(source.PublisherRequest(document_id=DOCUMENT, client_id='tab'))
    first = publish()
    payload = {'overleafProjectId': DOCUMENT, 'source_context_ref': first['source_context_ref']}
    command = companion_service.admit('/formalize/all', payload, 'original')['command']
    epoch = commands.start_worker(OWNER)
    claimed = commands.claim(OWNER, epoch, 'compute', kinds={'companion.dispatch'})
    assert claimed['id'] == command['id']
    return {'command': command, 'claimed': claimed, 'epoch': epoch, 'source': first,
            'payload': payload, 'context': {'source_context_ref': first['source_context_ref'], 'sourceBundle': bundle()}}


def renew(client='tab'):
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until='2000-01-01'")
    renewed = source.publisher(source.PublisherRequest(
        document_id=DOCUMENT, client_id=client, expected_generation=1))
    assert renewed['generation'] == 2


def test_review_admission_uses_renewed_editor_source_and_withdrawal_needs_no_lease(accepted):
    request = {'overleafProjectId': DOCUMENT, 'target': {'overleafProjectId': DOCUMENT,
        'source_context_ref': accepted['source']['source_context_ref']},
        'decision': {'action': 'approve', 'outcome': 'proved', 'revision_token': 'reviewed'}}
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until='2000-01-01'")
    with pytest.raises(HTTPException) as stale:
        companion_service.admit('/lean-pane/review/decision', request, 'expired-review')
    assert stale.value.status_code == 409
    renew()
    current = publish(generation=2)
    request['target']['source_context_ref'] = current['source_context_ref']
    admitted = companion_service.admit('/lean-pane/review/decision', request, 'fresh-review')['command']
    with db.connect() as conn:
        assert conn.execute('select publication_id from companion_source_bindings where command_id=?',
                            (admitted['id'],)).fetchone()[0] == current['source_context_ref']
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until='2000-01-01'")
    withdrawal = companion_service.admit('/lean-pane/review/decision',
        {'overleafProjectId': DOCUMENT, 'decision': {'action': 'withdraw', 'approval_id': 'approval'}},
        'withdraw-without-source')['command']
    with db.connect() as conn:
        assert not conn.execute('select 1 from companion_source_bindings where command_id=?', (withdrawal['id'],)).fetchone()


def api_result(accepted, *, context=None):
    marker = db.caller_parent_command.set(accepted['command']['id'])
    try:
        with db.read_snapshot() as conn:
            return source.resolve(conn, (context or accepted['context'])['source_context_ref'])
    finally:
        db.caller_parent_command.reset(marker)


def worker_result(accepted, *, context=None, parent=True, epoch=None):
    # Use the actual migrated SQLite schema and worker code, without HTTP or Lea.
    script = """
        import fs from 'node:fs';
        const input=JSON.parse(fs.readFileSync(0,'utf8'));
        const {openWorkerDatabase}=await import(input.module);
        const db=openWorkerDatabase({...input,fetchImpl(){throw new Error('Unexpected HTTP access');}});
        try {
          const validate=()=>db.validateSource(input.document,input.sourceContext);
          const result=input.parent?db.runAccepted(input.parent,validate):validate();
          process.stdout.write(JSON.stringify(result));
        } catch(error) {process.stdout.write(JSON.stringify({rejected:error.message}));}
        finally {db.close();}
    """
    result = subprocess.run(['node', '--input-type=module', '-e', script], text=True,
                            input=json.dumps({'module': WORKER_MODULE.as_uri(), 'filename': str(db.DB_PATH),
                                              'owner': OWNER, 'epoch': epoch or accepted['epoch'],
                                              'baseUrl': 'http://offline.invalid', 'leaRoot': str(db.DB_PATH.parent),
                                              'document': DOCUMENT, 'sourceContext': context or accepted['context'],
                                              'parent': accepted['command']['id'] if parent else None}),
                            capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize('state', ['claimed', 'succeeded'])
@pytest.mark.parametrize('client', ['tab', 'replacement-tab'])
@pytest.mark.parametrize('republish', [False, True])
def test_accepted_input_survives_publisher_renewal(accepted, monkeypatch, state, client, republish):
    if state == 'succeeded':
        with db.write() as conn:
            commands._settle(conn, accepted['claimed'], 'succeeded', {'batchId': 'retained-batch'})
    renew(client)
    if republish:
        current = publish(client=client, generation=2)
        assert current['source_context_ref'] != accepted['source']['source_context_ref']
        assert current['source_archive_ref'] == accepted['source']['source_archive_ref']
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('Source resolution touched the workspace'))
        assert api_result(accepted)['bundleHash'] == accepted['context']['sourceBundle']['bundleHash']
    assert worker_result(accepted)['ok'] is True
    # The accepted operation can resume; a fresh public request cannot use an old publisher.
    with db.read_snapshot() as conn:
        with pytest.raises(ValueError, match='publisher generation changed'):
            source.resolve(conn, accepted['source']['source_context_ref'])
    assert worker_result(accepted, parent=False)['error'] == 'publisher_conflict'
    with pytest.raises(HTTPException) as rejected:
        companion_service.admit('/formalize/all', accepted['payload'], 'new-submission')
    assert rejected.value.status_code == 409
    assert companion_service.admit('/formalize/all', accepted['payload'], 'original')['command']['id'] == accepted['command']['id']
    with pytest.raises(HTTPException):
        publish(sequence=50)  # The old browser cannot write into generation 2.


def test_expiry_alone_does_not_revoke_accepted_input(accepted):
    with db.write() as conn:
        conn.execute("update source_publishers set lease_until='2000-01-01'")
    assert api_result(accepted)['statement'] == 'Statement'
    assert worker_result(accepted)['ok'] is True
    assert worker_result(accepted, parent=False)['error'] == 'publisher_conflict'
    with db.read_snapshot() as conn:
        with pytest.raises(ValueError, match='stale'):
            source.resolve(conn, accepted['source']['source_context_ref'])


@pytest.mark.parametrize('change', ['statement', 'target-archive', 'other-target-archive', 'coverage'])
def test_accepted_input_still_rejects_changed_content(accepted, change):
    renew()
    options = {'statement': 'Changed'} if change == 'statement' else (
        {'key': 'other', 'text': 'changed surrounding input'} if change == 'other-target-archive' else (
            {'coverage': 'partial'} if change == 'coverage' else {'text': 'changed surrounding input'}))
    publish(generation=2, **options)
    with pytest.raises(ValueError, match='changed'):
        api_result(accepted)
    assert worker_result(accepted)['error'] == 'published_source_changed'


def test_accepted_command_cannot_borrow_an_unbound_publication(accepted):
    current = publish(sequence=2, key='other')
    context = {'source_context_ref': current['source_context_ref'], 'sourceBundle': bundle('other')}
    with pytest.raises(ValueError, match='not frozen'):
        api_result(accepted, context=context)
    assert worker_result(accepted, context=context)['error'] == 'published_source_required'


@pytest.mark.parametrize('state', ['cancelled', 'expired', 'failed'])
def test_terminal_failure_cannot_reuse_the_binding(accepted, state):
    with db.write() as conn:
        commands._settle(conn, accepted['claimed'], state)
    renew()
    with pytest.raises(ValueError, match='not frozen'):
        api_result(accepted)
    assert 'reconciliation' in worker_result(accepted)['rejected']


def test_worker_epoch_still_fences_accepted_source(accepted):
    renew()
    assert 'epoch expired' in worker_result(accepted, epoch='obsolete-worker')['rejected']


def admitted_solver(accepted, monkeypatch):
    from app.config import LeaConfig
    from app.projects import project_repo_dir
    import app.config
    root = db.DB_PATH.parent
    monkeypatch.setattr(app.config, 'load_config', lambda: LeaConfig(model='test', lea_root=root, max_turns=3))
    project = store.create_project(DOCUMENT)
    form = store.create_formalization(project_id=project['id'], loose_session_id=None, display_title='Target',
                                      origin='overleaf', origin_key=f'{DOCUMENT}:theorem:target')
    with db.write() as conn:
        commands._settle(conn, accepted['claimed'], 'succeeded', {'batchId': 'retained-batch'})
    marker = db.caller_parent_command.set(accepted['command']['id'])
    try:
        created = store.create_run_bundle(message='test', session_id=None, project_id=project['id'],
            session_origin='overleaf', session_origin_url=None, model='test', provider=None, max_turns=3,
            autonomous=True, focus_formalization_id=form['id'], source_bundle=bundle(),
            source_context_ref=accepted['source']['source_context_ref'])
    finally:
        db.caller_parent_command.reset(marker)
    token = commands.claim(OWNER, accepted['epoch'], 'compute', kinds={'run.execute'})
    assert token
    proofs_root = root / 'workspace/proofs'
    repo = project_repo_dir(project, proofs_root)
    repo.mkdir(parents=True)
    return created['run'], token, proofs_root, repo


@pytest.mark.parametrize('client', ['tab', 'replacement-tab'])
def test_admitted_solver_materializes_after_http_parent_context_ends(accepted, monkeypatch, client):
    run, token, proofs_root, repo = admitted_solver(accepted, monkeypatch)
    renew(client)
    publish(client=client, generation=2)
    assert db.caller_parent_command.get() is None
    from app.overleaf_source import validate_run
    with run_commands.executing(token):
        validate_run(run['id'], proofs_root)
    assert (repo / '.lea/files/overleaf/main.tex').read_text() == 'immutable editor bytes'
    with db.read_snapshot() as conn:
        assert conn.execute("select count(*) from workspace_effects where state='published'").fetchone()[0] == 1


@pytest.mark.parametrize('change', ['statement', 'target-archive', 'other-target-archive'])
def test_admitted_solver_rejects_real_source_changes_after_renewal(accepted, monkeypatch, change):
    run, token, proofs_root, repo = admitted_solver(accepted, monkeypatch)
    renew()
    options = {'statement': 'Changed'} if change == 'statement' else (
        {'key': 'other', 'text': 'changed surrounding input'} if change == 'other-target-archive'
        else {'text': 'changed surrounding input'})
    publish(generation=2, **options)
    with run_commands.executing(token), pytest.raises(ValueError, match='changed'):
        source.materialize_run(run['id'], proofs_root)
    assert not (repo / '.lea/files/overleaf/main.tex').exists()


@pytest.mark.parametrize('ownership', ['missing', 'lost'])
def test_materialization_requires_the_owned_run_even_with_a_retained_source(accepted, monkeypatch, ownership):
    run, token, proofs_root, repo = admitted_solver(accepted, monkeypatch)
    renew()
    if ownership == 'lost':
        with db.write() as conn:
            commands._settle(conn, token, 'cancelled')
        with run_commands.executing(token), pytest.raises(ValueError, match='owned worker command'):
            source.materialize_run(run['id'], proofs_root)
    else:
        with pytest.raises(ValueError, match='owned worker command'):
            source.materialize_run(run['id'], proofs_root)
    assert not (repo / '.lea/files/overleaf/main.tex').exists()
