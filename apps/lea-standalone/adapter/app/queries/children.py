"""Durable child observations for a coordinator snapshot. SQL is the sole source."""
import json
from datetime import datetime, timezone

from ..db import read_snapshot


def _timestamp(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def observations_for_session(session_id):
    with read_snapshot() as conn:
        # Retained events also recover observations written before per-kind records.
        # Text is read once from its canonical timeline row, never replayed here.
        rows = conn.execute('''select e.event_id as seq,e.run_id,e.attempt_id,e.type,e.payload,
            json_extract(e.payload,'$.created_at') as observed_at
            from run_observation_records e join runs r on r.id=e.run_id
            where r.session_id=? and e.type like 'subagent_%'
            union all
            select e.id,e.run_id,e.attempt_id,e.type,e.payload,e.created_at
            from run_events e join runs r on r.id=e.run_id
            where r.session_id=? and e.type like 'subagent_%'
              and not (e.type='subagent_progress' and json_extract(e.payload,'$.kind')='text')
              and e.id in (select max(id) from run_events where run_id=e.run_id
                and type like 'subagent_%' group by attempt_id,type,json_extract(payload,'$.child_id'),
                  case when type='subagent_progress' then json_extract(payload,'$.kind') end)
            order by seq''', (session_id, session_id)).fetchall()
        child_runs = conn.execute('''select s.id,r.status from sessions s left join runs r
            on r.id=(select id from runs where session_id=s.id order by created_at desc,id desc limit 1)
            where s.parent_id=?''', (session_id,)).fetchall()
        children = {row['id']: {'child_id': row['id'], 'progress': {'text': ''}} for row in child_runs}
        for row in rows:
            payload = json.loads(row['payload'])
            child_id = payload.get('child_id')
            if not child_id:
                continue
            child = children.setdefault(child_id, {'child_id': child_id, 'progress': {'text': ''}})
            child.update(parent_run_id=row['run_id'], attempt_id=row['attempt_id'])
            kind, progress = payload.get('kind'), child['progress']
            if row['type'] == 'subagent_started':
                child['queued'] = payload.get('state') == 'queued'
            elif row['type'] == 'subagent_running':
                child['queued'] = False
            elif row['type'] == 'subagent_finished':
                child.update(queued=False, finished=True, error=payload.get('error'),
                             stop_notice=payload.get('stop_notice'), stop_reason=payload.get('stop_reason'))
            elif row['type'] == 'subagent_progress':
                if kind == 'turn':
                    progress.update(turn=payload.get('turn'), text='')
                    progress.pop('tool', None)
                    child['turn_at'] = row['observed_at']
                    child['message_frontier'] = payload.get('message_frontier')
                elif kind == 'tool':
                    progress['tool'] = payload.get('tool')
                elif kind == 'check':
                    progress['check'] = payload.get('status')
                    progress.pop('tool', None)
        for row in child_runs:
            if row['status'] and row['status'] not in {'pending', 'running'}:
                children[row['id']].update(finished=True, queued=False)
        # Correlated maxima keep the projection bounded to one narration per child.
        messages = conn.execute('''select t.* from timeline t join sessions s on s.id=t.session_id
            where s.parent_id=? and t.kind='message' and t.author='agent'
            and t.id=(select max(m.id) from timeline m where m.session_id=s.id
                and m.kind='message' and m.author='agent')''', (session_id,)).fetchall()
        for message in messages:
            child = children.get(message['session_id'])
            if not child or child.get('finished'):
                continue
            # A new turn without narration must not resurrect the preceding turn.
            turn_at = _timestamp(child.get('turn_at'))
            frontier = child.get('message_frontier')
            if frontier is not None and message['id'] <= frontier:
                continue
            if turn_at is not None and _timestamp(message['created_at']) < turn_at:
                continue
            child['progress'].update(text=message['content'], message_id=str(message['id']),
                                     stream_sequence=message['stream_sequence'])
        for child in children.values():
            child.pop('turn_at', None)
            child.pop('message_frontier', None)
        return list(children.values())
