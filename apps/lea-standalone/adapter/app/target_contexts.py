"""Worker-owned, durable target agreements in existing message/invocation records.

Proposals are metadata on their exact assistant message. A confirming run pins an
immutable context invocation before execution. No UI callback or workspace scan is
involved, and replay cannot silently replace an established target.
"""
import hashlib
import json
import re

from . import db
from .artifacts import declaration_signature, scan_lean_declarations

_FENCE = re.compile(r'```(?:lean|lean4)\s*\n(.*?)```', re.S)
_AFFIRMATIONS = {
    'yes', 'yes it does', 'yes that is correct', 'yes that matches', 'yes please',
    'yes prove it', 'yes go ahead', 'yes please proceed', 'yes proceed',
    'correct', 'that is correct', "that's correct", 'that is right', "that's right",
    'sounds good', 'looks good', 'go ahead', 'please proceed', 'proceed',
    'please prove it', 'prove it', 'yes it matches', 'yes that works',
    'ok', 'okay', "let's do it", "yes that's correct", "yes that's right",
}


def _affirmation(text):
    reply = re.sub(r"[^\w\s']", ' ', text.lower().replace('’', "'"))
    reply = ' '.join(reply.split())
    if reply in _AFFIRMATIONS:
        return True
    for ending in ('please proceed', 'go ahead', 'please prove it', 'prove it', "let's do it"):
        if reply.endswith(' ' + ending):
            prefix = reply[:-len(ending)].strip()
            if prefix.endswith(' and'):
                prefix = prefix[:-4].strip()
            if prefix in _AFFIRMATIONS:
                return True
    return False


def _retry(text):
    reply = ' '.join(re.sub(r'[^\w\s]', ' ', text.lower()).split())
    reply = re.sub(r'^please | please$', '', reply)
    return reply in {'try again', 'retry', 'continue', 'continue the proof',
                     'finish the proof', 'check it again', 'check the proof again'}


def _asks_confirmation(text):
    prose = _FENCE.sub('', text)
    return '?' in prose and bool(re.search(r'propos|formulat|statement|match|correct|prove|formaliz', prose, re.I))


def _owns(conn, run, token):
    if token:
        from .run_commands import owns
        if not owns(conn, run['id'], token):
            raise RuntimeError('Obsolete solver command cannot establish a target')
    elif conn.execute('select 1 from run_commands where run_id=?', (run['id'],)).fetchone():
        raise RuntimeError('Target context requires the owning solver command')


def _proposal(text, kind):
    candidates = []
    for code in _FENCE.findall(text or ''):
        declarations = scan_lean_declarations(code)
        if len(declarations) != 1:
            continue
        decl = declarations[0]
        if (kind == 'definition') != (decl.kind == 'definition'):
            continue
        signature = declaration_signature(decl)
        if signature:
            candidates.append({'declaration_name': decl.full_name, 'statement': signature,
                               'proposed_source': code.strip(), 'kind': kind})
    return candidates[0] if len(candidates) == 1 else None


def _request(conn, run, before):
    row = conn.execute('''select id,content from timeline where session_id=? and
        formalization_id=? and kind='message' and author='user' and id<=?
        order by id limit 1''', (run['session_id'], run['focus_formalization_id'], before)).fetchone()
    return dict(row) if row else None


def record_proposal(run_id, message_id=None, *, token=None):
    """Only a final, unambiguous proposal for an unnamed chat target is eligible."""
    with db.write() as conn:
        run = conn.execute('select * from runs where id=?', (run_id,)).fetchone()
        if not run or not run['focus_formalization_id']:
            return
        target = conn.execute('select * from formalizations where id=?', (run['focus_formalization_id'],)).fetchone()
        if not target or target['declaration_name'] or target['origin'] != 'ui':
            return
        _owns(conn, run, token)
        if message_id is None:
            latest = conn.execute("select id from timeline where run_id=? and author='agent' and kind='message' order by id desc limit 1",
                                  (run_id,)).fetchone()
            message_id = latest[0] if latest else None
        message = conn.execute("select * from timeline where id=? and run_id=? and author='agent' and kind='message'",
                               (message_id, run_id)).fetchone()
        if not message or not (proposal := _proposal(message['content'], target['kind'])):
            return
        request = _request(conn, run, message['id'])
        if not request:
            return
        proposal.update(request=request['content'], request_message_id=request['id'],
                        proposal_message_id=message['id'], formalization_id=target['id'])
        data = json.loads(message['data'] or '{}')
        data['target_proposal'] = proposal
        conn.execute('update timeline set data=? where id=?', (json.dumps(data), message['id']))


def load(run_id):
    with db.connect() as conn:
        row = conn.execute("select result_json from run_invocations where id=? and kind='context' and name='confirmed_target'",
                           ('target_context:' + run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def prepare(run_id, *, token=None):
    """Pin the admitted target; a plain affirmation accepts only the latest proposal.

    Qualified replies (for example, 'yes, but use integers') do not establish the
    proposed statement. A revised, unambiguous proposal needs fresh confirmation.
    """
    with db.write() as conn:
        run = conn.execute('select * from runs where id=?', (run_id,)).fetchone()
        if not run or not run['focus_formalization_id']:
            return None
        target = conn.execute('select * from formalizations where id=?', (run['focus_formalization_id'],)).fetchone()
        # Published Overleaf targets already have their immutable SQL source
        # context and confirmation protocol; do not replace that authority.
        if not target or target['origin'] != 'ui':
            return None
        _owns(conn, run, token)
        if existing := load(run_id):
            return existing
        message = conn.execute("select * from timeline where run_id=? and kind='message' and author='user' order by id limit 1",
                               (run_id,)).fetchone()
        if not target or not message:
            return None
        request = _request(conn, run, message['id'])
        # A continuation retains the exact context/configuration that its saved
        # execution position used, including the original confirmation provenance.
        context = load(run['resumes_run_id']) if run['resumes_run_id'] else None
        if context and context.get('formalization_id') != target['id']:
            raise RuntimeError('A continued run cannot inherit a different confirmed target')
        if not context and target['declaration_name']:
            context = {'declaration_name': target['declaration_name'], 'statement': target['statement'],
                       'kind': target['kind'], 'request': request['content'] if request else target['statement'] or target['display_title'],
                       'confirmation': 'established_target'}
        elif not context:
            # Also recover an agreement from an older, already completed run.
            # This repairs context on the next worker activation, without
            # relabelling historical results or asking the model to infer consent.
            messages = conn.execute("""select * from timeline where session_id=? and formalization_id=?
                and kind='message' and author='user' and id<=? order by id desc limit 100""",
                (run['session_id'], target['id'], message['id'])).fetchall()
            for confirmation in messages:
                if not _affirmation(confirmation['content']):
                    # A new request/revision blocks older agreements. Historical
                    # recovery is limited to explicit continuation/retry turns.
                    if _retry(confirmation['content']):
                        continue
                    break
                prior = conn.execute("""select * from timeline where session_id=? and formalization_id=?
                    and kind='message' and author='agent' and id<? order by id desc limit 1""",
                    (run['session_id'], target['id'], confirmation['id'])).fetchone()
                if not prior:
                    continue
                if not _asks_confirmation(prior['content']):
                    break
                # Reparse the exact visible proposal so edited/stale message
                # metadata cannot establish a different statement. This also
                # supports conversations created before proposal annotation.
                proposal = _proposal(prior['content'], target['kind'])
                if proposal and request:
                    proposal.update(request=request['content'], request_message_id=request['id'])
                    context = {**proposal, 'proposal_message_id': prior['id'],
                               'confirmation_message_id': confirmation['id'], 'confirmation': 'user_message'}
                    conn.execute('''update formalizations set declaration_name=?,statement=?,updated_at=?
                        where id=? and declaration_name is null''',
                        (context['declaration_name'], context['statement'], db.utc_now(), target['id']))
                    break
                # Do not let a newer ambiguous agreement select an older target.
                break
        if not context:
            return None
        if 'sha256' not in context:
            context.update(version=1, formalization_id=target['id'], source_hash=target['source_hash'])
            raw = json.dumps(context, sort_keys=True, ensure_ascii=False)
            context['sha256'] = hashlib.sha256(raw.encode()).hexdigest()
        now = db.utc_now()
        conn.execute('''insert into run_invocations(id,run_id,attempt_id,kind,name,turn,input_json,
            result_json,started_at,completed_at) values (?,?,?,'context','confirmed_target',0,?,?,?,?)''',
            ('target_context:' + run_id, run_id, run['attempt_id'], json.dumps({'message_id': message['id']}),
             json.dumps(context, ensure_ascii=False), now, now))
        return context


def unchanged(context):
    if not context:
        return True
    with db.connect() as conn:
        target = conn.execute('select declaration_name,statement,source_hash,kind from formalizations where id=?',
                              (context['formalization_id'],)).fetchone()
    return bool(target and all(target[k] == context.get(k) for k in ('declaration_name','statement','source_hash','kind')))
