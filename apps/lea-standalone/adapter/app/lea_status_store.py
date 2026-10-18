"""Append-only assessments. All sequence and merge operations use db.write()."""
from __future__ import annotations
import json
from uuid import uuid4

from .db import connect, write, utc_now


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result


def admit(conn, run_id, formalization_id, session_id, project_id, operation, source):
    if not formalization_id:
        raise ValueError("Lea Status requires a bound formalization")
    form = conn.execute("select * from formalizations where id = ?", (formalization_id,)).fetchone()
    if not form or form["origin"] != "overleaf":
        raise ValueError("Lea Status requires an Overleaf formalization")
    if form["origin_key"] and not form["origin_key"].endswith(f":{source['targetKind']}:{source['targetKey']}"):
        raise ValueError("Source bundle belongs to a different target")
    if form["origin_key"]:
        from .overleaf_source import bind
        bind(conn, dict(form), source)
    generation = conn.execute("select coalesce(max(run_generation), 0) + 1 from lea_status_run_contexts where formalization_id = ?", (formalization_id,)).fetchone()[0]
    prior = conn.execute("""select u.id from lea_status_updates u join lea_status_run_contexts c on c.run_id = u.run_id
        where u.formalization_id = ? order by c.run_generation desc, u.sequence desc limit 1""", (formalization_id,)).fetchone()
    conn.execute("""insert into lea_status_run_contexts
        (run_id, formalization_id, session_id, project_id, operation, run_generation,
         source_bundle_json, source_identity_hash, source_bundle_hash, inherited_update_id, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, formalization_id, session_id, project_id, operation, generation,
         json.dumps(source, ensure_ascii=False), source["sourceIdentityHash"], source["bundleHash"], prior[0] if prior else None, utc_now()))


def context(run_id):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_run_contexts where run_id = ?", (run_id,)).fetchone())


def latest(run_id):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_updates where run_id = ? order by sequence desc limit 1", (run_id,)).fetchone())


def get(update_id):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_updates where id = ?", (update_id,)).fetchone())


def invocation(run_id, invocation_key):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_updates where run_id = ? and invocation_key = ?", (run_id, invocation_key)).fetchone())


def append(run_id, invocation_key, payload, snapshot, dependency_hash):
    from lea.status_reporting import merge_assessment, validate_payload
    payload = validate_payload(payload)
    with write() as conn:
        prior_call = unpack(conn.execute("select * from lea_status_updates where run_id = ? and invocation_key = ?", (run_id, invocation_key)).fetchone())
        if prior_call:
            if prior_call["payload"] != payload:
                raise ValueError("Invocation identity already used with different content")
            return prior_call
        ctx = unpack(conn.execute("select * from lea_status_run_contexts where run_id = ?", (run_id,)).fetchone())
        if not ctx:
            raise ValueError("Run has no reporting context")
        run = conn.execute("select status from runs where id = ?", (run_id,)).fetchone()
        if not run or run[0] not in {"pending", "running"}:
            raise ValueError("Run is no longer active")
        previous = unpack(conn.execute("select * from lea_status_updates where run_id = ? order by sequence desc limit 1", (run_id,)).fetchone())
        if not previous and ctx["inherited_update_id"]:
            previous = unpack(conn.execute("select * from lea_status_updates where id = ?", (ctx["inherited_update_id"],)).fetchone())
        assessment = merge_assessment(previous["assessment"] if previous else None, payload, ctx["source_identity_hash"])
        sequence = conn.execute("select coalesce(max(sequence), 0) + 1 from lea_status_updates where formalization_id = ?", (ctx["formalization_id"],)).fetchone()[0]
        update_id = str(uuid4())
        conn.execute("""insert into lea_status_updates
            (id, run_id, formalization_id, sequence, invocation_key, kind, payload_json,
             assessment_json, artifact_snapshot_json, dependency_hash, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (update_id, run_id, ctx["formalization_id"], sequence, invocation_key, payload["kind"],
             json.dumps(payload, ensure_ascii=False), json.dumps(assessment, ensure_ascii=False),
             json.dumps(snapshot), dependency_hash, utc_now()))
        return unpack(conn.execute("select * from lea_status_updates where id = ?", (update_id,)).fetchone())


def selected_context(formalization_id):
    with connect() as conn:
        return unpack(conn.execute("""select c.* from lea_status_run_contexts c where formalization_id = ?
          and (operation != 'overleaf_continuation'
            or exists(select 1 from lea_status_updates u where u.run_id = c.run_id)
            or exists(select 1 from timeline s where s.kind = 'code' and s.run_id = c.run_id and s.formalization_id = c.formalization_id))
          order by run_generation desc limit 1""", (formalization_id,)).fetchone())


def history(formalization_id, after=0, limit=50):
    with connect() as conn:
        rows = conn.execute("""select u.*, c.run_generation, c.source_identity_hash, c.source_bundle_hash
            from lea_status_updates u join lea_status_run_contexts c on c.run_id = u.run_id
            where u.formalization_id = ? and u.sequence > ? order by u.sequence limit ?""",
            (formalization_id, after, limit + 1)).fetchall()
    return [unpack(r) for r in rows[:limit]], len(rows) > limit
