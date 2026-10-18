"""SQL-only observation primitives. No worker, filesystem, provider, or UI imports."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db
from .migration_limits import CHANGE_PAGE_BYTES, CHANGE_PAGE_DEFAULT, CHANGE_PAGE_MAX, CHANGE_REPLAY_SECONDS


class CursorReset(ValueError):
    def __init__(self, floor: int, watermark: int):
        self.floor, self.watermark = floor, watermark
        super().__init__("Change cursor is outside retained history; reload a snapshot")


class RunCursorReset(CursorReset):
    pass


def _record_event(conn, run_id, attempt, event_id, event_type, payload):
    # Text/code already have canonical IDs and blobs. Transient status keeps its
    # latest value; approvals/diagnostics retain their distinct audit identities.
    if event_type in {"assistant_delta", "message", "code_step", "resync"}:
        return
    if event_type == "subagent_progress" and payload.get("kind") == "text":
        return
    key = str(payload.get("approval_id") or payload.get("id") or payload.get("child_id") or "latest")
    conn.execute("""insert into run_observation_records values (?,?,?,?,?,?)
        on conflict(run_id,attempt_id,type,record_key) do update set
        event_id=excluded.event_id,payload=excluded.payload where excluded.event_id>event_id""",
        (run_id, attempt, event_type, key, event_id, json.dumps(payload, ensure_ascii=False)))


def run_records(run_id):
    with db.connect() as conn:
        return [{"seq": r["event_id"], "type": r["type"], "payload": json.loads(r["payload"])}
                for r in conn.execute("select * from run_observation_records where run_id=? order by event_id", (run_id,))]


def watermark(conn) -> int:
    # sqlite_sequence survives retention of every row, so cursors never go backwards.
    row = conn.execute("select seq from sqlite_sequence where name='domain_changes'").fetchone()
    return row[0] if row else 0


def changes(after: int, *, limit: int = CHANGE_PAGE_DEFAULT, project_id: str | None = None,
            session_id: str | None = None, formalization_id: str | None = None,
            run_id: str | None = None) -> dict:
    if after < 0 or not 1 <= limit <= CHANGE_PAGE_MAX:
        raise ValueError("Invalid change cursor or page size")
    with db.read_snapshot() as conn:
        high = watermark(conn)
        floor = conn.execute("select floor from change_retention where id=1").fetchone()[0]
        if after < floor or after > high:
            raise CursorReset(floor, high)
        filters, args = ["id > ?", "id <= ?"], [after, high]
        for column, value in (("project_id", project_id), ("session_id", session_id),
                              ("formalization_id", formalization_id), ("run_id", run_id)):
            if value is not None:
                filters.append(f"{column} = ?")
                args.append(value)
        rows = conn.execute(f"select * from domain_changes where {' and '.join(filters)} order by id limit ?",
                            (*args, limit + 1)).fetchall()
        page, size = [], 0
        for row in rows[:limit]:
            item = dict(row)
            item["entity_revision"] = item["id"]  # committed invalidation revision, not message seq
            cost = len(json.dumps(item).encode())
            if size + cost > CHANGE_PAGE_BYTES:
                if not page:
                    raise CursorReset(floor, high)
                break
            page.append(item)
            size += cost
        more = len(rows) > len(page)
        cursor = page[-1]["id"] if more and page else high
        return {"changes": page, "cursor": cursor, "watermark": high, "has_more": more,
                "retained_after": floor}


def session_snapshot(session_id: str) -> dict | None:
    """Versioned SQL snapshot for new observers. Code bytes use blob references.

    Deliberately does not call legacy session_detail: its live freshness verdicts
    still inspect the workspace until Stage 2. No transcript/checkpoint/tool config
    secrets are exposed here. Existing session routes retain their compatibility shape.
    """
    with db.read_snapshot() as conn:
        high = watermark(conn)  # pins the same read version used by every query below
        session = conn.execute("select * from sessions where id=?", (session_id,)).fetchone()
        if session is None:
            return None
        rows = [dict(r) for r in conn.execute("""select id,session_id,run_id,formalization_id,kind,author,
            content,turn,path,after_blob_id,summary,check_status,check_detail,artifact_kind,content_lost,
            stream_state,stream_attempt_id,stream_sequence,created_at from timeline
            where session_id=? order by id""", (session_id,))]
        runs = [dict(r) for r in conn.execute("""select id,session_id,project_id,status,attempt_id,
            result_kind,stop_reason,stop_requested_reason,usage_status,usage_revision,input_tokens,
            output_tokens,cost_usd,pending_approval,created_at,updated_at from runs
            where session_id=? order by created_at,id""", (session_id,))]
        records = [{**item, "run_id": run['id']} for run in runs for item in run_records(run['id'])]
        return {"version": 1, "cursor": high, "session": dict(session), "timeline": rows, "runs": runs,
                "run_records": records}


def append_run_event(run_id: str, event_type: str, payload: dict) -> dict:
    """Commit before any legacy or durable subscriber can observe the event."""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > CHANGE_PAGE_BYTES:
        # Canonical code/messages are already stored. Do not duplicate huge blobs
        # into the replay log; clients reload their committed snapshot instead.
        event_type, payload = "resync", {"run_id": run_id, "reload": True,
                                        "reason": "event_payload_exceeds_replay_budget"}
        encoded = json.dumps(payload)
    with db.write() as conn:
        run = conn.execute("select attempt_id from runs where id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Cannot publish an event for a missing run")
        attempt = run[0] or ""
        cursor = conn.execute("insert into run_events(run_id,attempt_id,type,payload) values (?,?,?,?)",
                              (run_id, attempt, event_type, encoded)).lastrowid
        conn.execute("""insert into run_event_retention(run_id,attempt_id,high_water) values (?,?,?)
            on conflict(run_id,attempt_id) do update set high_water=excluded.high_water""", (run_id, attempt, cursor))
        _record_event(conn, run_id, attempt, cursor, event_type, json.loads(encoded))
    return {"seq": cursor, "type": event_type, "payload": json.loads(encoded), "attempt_id": attempt}


def run_events(run_id: str, attempt_id: str | None, after: int, limit: int = CHANGE_PAGE_DEFAULT) -> list[dict]:
    if not 1 <= limit <= CHANGE_PAGE_MAX:
        raise ValueError("Invalid event page size")
    with db.read_snapshot() as conn:
        boundary = conn.execute("select floor,high_water from run_event_retention where run_id=? and attempt_id=?",
                                (run_id, attempt_id or "")).fetchone()
        floor, high = (boundary['floor'],boundary['high_water']) if boundary else (0,0)
        if after < floor or after > high:
            raise RunCursorReset(floor, high)
        # Read only sizes first. Loading 200 maximum-size payloads before applying
        # a 1 MiB page budget would allocate 200 MiB for one slow subscriber.
        metadata = conn.execute("""select id,length(cast(payload as blob)) as bytes from run_events
            where run_id=? and attempt_id=? and id>? order by id limit ?""",
            (run_id, attempt_id or "", max(after, 0), limit)).fetchall()
        size, end = 0, None
        for row in metadata:
            if size + row["bytes"] > CHANGE_PAGE_BYTES:
                if end is None:
                    return [{"seq": row["id"], "type": "resync", "payload": {
                        "run_id": run_id, "reload": True, "reason": "event_payload_exceeds_replay_budget"}}]
                break
            size += row["bytes"]
            end = row["id"]
        if end is None:
            return []
        rows = conn.execute("""select id,type,payload from run_events where run_id=? and attempt_id=?
            and id>? and id<=? order by id""", (run_id, attempt_id or "", max(after, 0), end)).fetchall()
        return [{"seq": row["id"], "type": row["type"], "payload": json.loads(row["payload"])} for row in rows]


def compact_changes(*, now: datetime | None = None, limit: int = 1000) -> int:
    """Explicit maintenance, never a GET side effect. Preserve a contiguous floor.

    Compact only invalidation metadata; canonical history is never pruned.
    """
    if not 1 <= limit <= CHANGE_PAGE_MAX:
        raise ValueError('Invalid compaction limit')
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(seconds=CHANGE_REPLAY_SECONDS)).strftime("%Y-%m-%dT%H:%M:%fZ")
    with db.write() as conn:
        first_new = conn.execute("select min(id) from domain_changes where created_at>=?", (cutoff,)).fetchone()[0]
        ceiling = first_new - 1 if first_new else watermark(conn)
        rows = conn.execute("select id from domain_changes where id<=? order by id limit ?", (ceiling, limit)).fetchall()
        previous = conn.execute("select floor from change_retention where id=1").fetchone()[0]
        floor = max(previous, rows[-1][0] if rows else previous)
        conn.execute("delete from domain_changes where id<=?", (floor,))
        conn.execute("update change_retention set floor=? where id=1", (floor,))
        return floor


def compact_run_events(*, now: datetime | None = None, limit: int = 200) -> dict:
    """Bounded worker maintenance. Never prune active or unpreserved text.

    Legacy deltas without canonical identities are protected until backfilled;
    checked_at rotates attempts so one protected legacy run cannot starve others.
    Canonical transcript/proof bytes and audit records outlive transport retention.
    """
    moment = now or datetime.now(timezone.utc)
    if not 1 <= limit <= CHANGE_PAGE_MAX:
        raise ValueError('Invalid compaction limit')
    cutoff = (moment - timedelta(seconds=CHANGE_REPLAY_SECONDS)).isoformat()
    removed, protected, used = 0, 0, 0
    with db.write() as conn:
        candidates = conn.execute("""select e.run_id,e.attempt_id from run_event_retention e
            join runs r on r.id=e.run_id where r.status not in ('pending','running')
            and julianday(r.updated_at)<julianday(?)
            and exists(select 1 from run_events v where v.run_id=e.run_id and v.attempt_id=e.attempt_id
                and julianday(v.created_at)<julianday(?))
            order by e.checked_at,e.run_id,e.attempt_id limit 20""", (cutoff, cutoff)).fetchall()
        for candidate in candidates:
            run_id, attempt = candidate
            conn.execute("update run_event_retention set checked_at=? where run_id=? and attempt_id=?",
                         (moment.isoformat(), run_id, attempt))
            # Select metadata before payloads; at most 1 MiB and `limit` records.
            metadata = conn.execute("""select id,length(cast(payload as blob)),created_at from run_events
                where run_id=? and attempt_id=? order by id limit ?""", (run_id, attempt, limit-removed)).fetchall()
            floor = None
            for event_id, size, created in metadata:
                if used + size > CHANGE_PAGE_BYTES:
                    if size > CHANGE_PAGE_BYTES:
                        protected += 1
                    break
                try:
                    created_at = datetime.fromisoformat(created.replace('Z','+00:00'))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                except (AttributeError, ValueError):
                    protected += 1
                    break
                if created_at >= datetime.fromisoformat(cutoff):
                    break
                used += size
                event = conn.execute("select type,payload from run_events where id=?", (event_id,)).fetchone()
                try:
                    payload = json.loads(event['payload'])
                except (TypeError, ValueError):
                    protected += 1
                    break
                if not isinstance(payload, dict):
                    protected += 1
                    break
                if (event['type'] == 'assistant_delta' or
                    (event['type'] == 'subagent_progress' and payload.get('kind') == 'text')):
                    message = payload.get('message') or {}
                    if not isinstance(message,dict) or not isinstance(message.get('stream_sequence'),int):
                        protected += 1
                        break
                    message_run = run_id
                    if event['type']=='subagent_progress':
                        message_run=message.get('run_id')
                        child=conn.execute('''select 1 from runs child join sessions s on s.id=child.session_id
                            join runs parent on parent.session_id=s.parent_id
                            where child.id=? and parent.id=? and s.id=?''',
                            (message_run,run_id,payload.get('child_id'))).fetchone()
                        if not child:
                            protected += 1
                            break
                    saved = conn.execute("""select stream_sequence,stream_state,stream_attempt_id from timeline
                        where id=? and run_id=?""", (message.get('id'), message_run)).fetchone()
                    if (not saved or saved['stream_state'] not in {'final','interrupted'}
                        or saved['stream_sequence'] < message.get('stream_sequence', 0)
                        or saved['stream_attempt_id'] != (message.get('stream_attempt_id') or attempt)):
                        protected += 1
                        break
                _record_event(conn, run_id, attempt, event_id, event['type'], payload)
                floor = event_id
                removed += 1
            if floor is not None:
                conn.execute("delete from run_events where run_id=? and attempt_id=? and id<=?", (run_id, attempt, floor))
                conn.execute("update run_event_retention set floor=max(floor,?) where run_id=? and attempt_id=?",
                             (floor, run_id, attempt))
            if removed >= limit or used >= CHANGE_PAGE_BYTES:
                break
    return {"removed": removed, "protected": protected}
