import json
from ..db import connect

def unpack(row):
    if row is None:
        return None
    result = dict(row)
    for key in list(result):
        if key.endswith("_json"):
            result[key[:-5]] = json.loads(result.pop(key))
    return result

def context(run_id):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_run_contexts where run_id = ?", (run_id,)).fetchone())

def latest(run_id):
    with connect() as conn:
        return unpack(conn.execute("select * from lea_status_updates where run_id = ? order by sequence desc limit 1", (run_id,)).fetchone())

def get(update_id):
    with connect() as conn:
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
