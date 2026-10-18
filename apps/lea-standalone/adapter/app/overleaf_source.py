"""Published source bindings plus read-only provenance for pre-migration runs."""
from __future__ import annotations

import json
import hashlib

from . import db



def bind(conn, formalization: dict, source: dict | None = None) -> dict:
    if not formalization or formalization.get("origin") != "overleaf":
        raise ValueError("Select an Overleaf formalization to connect its source.")
    existing = conn.execute("select * from overleaf_bindings where formalization_id = ?",
                            (formalization["id"],)).fetchone()
    if existing:
        result = dict(existing)
        if source and (result["target_kind"], result["target_key"]) != (source["targetKind"], source["targetKey"]):
            raise ValueError("Source belongs to a different Overleaf target.")
        return result
    # Backfill from the trusted origin key using the exact target suffix, so
    # labels containing ':' remain intact. No guessing from chat text or title.
    if source is None:
        row = conn.execute("select source_bundle_json from lea_status_run_contexts where formalization_id = ? order by run_generation desc limit 1",
                           (formalization["id"],)).fetchone()
        source = json.loads(row[0]) if row else None
    if not source:
        raise ValueError("This target has no Overleaf source binding. Open it in the Overleaf pane first.")
    suffix = f":{source['targetKind']}:{source['targetKey']}"
    origin_key = formalization.get("origin_key") or ""
    document = origin_key[:-len(suffix)] if origin_key.endswith(suffix) else ""
    if not document or document == "unknown":
        raise ValueError("The Overleaf document for this target could not be identified.")
    conn.execute("insert into overleaf_bindings values (?, ?, ?, ?)",
                 (formalization["id"], document, source["targetKind"], source["targetKey"]))
    return dict(formalization_id=formalization["id"], document_id=document,
                target_kind=source["targetKind"], target_key=source["targetKey"])


def resolve(conn, reference: str, formalization_id: str, *, bind_source: bool = True) -> dict:
    if conn.execute('select 1 from source_publications where id=?',(reference,)).fetchone():
        from .source_publications import resolve as published
        from .reformalization_intake import actor
        return published(conn,reference,formalization_id,admission=not bool(actor()))
    from .reformalization_intake import actor
    if not actor():raise ValueError('Publish this target from Overleaf before sending; callback source references are retired.')
    row = conn.execute("select * from overleaf_source_requests where id = ?", (reference,)).fetchone()
    if not row or row["formalization_id"] != formalization_id or row["state"] != "ready" or row["expires_at"] <= db.utc_now():
        raise ValueError("Current Overleaf source is unavailable or expired. Refresh and send again.")
    source = json.loads(row["source_bundle_json"])
    latest = conn.execute("select source_bundle_json from overleaf_source_requests where formalization_id = ? and state = 'ready' order by captured_at desc limit 1",
                          (formalization_id,)).fetchone()
    if latest and json.loads(latest[0])["bundleHash"] != source["bundleHash"]:
        raise ValueError("Overleaf source changed before submission. Refresh and send again.")
    form = conn.execute("select * from formalizations where id = ?", (formalization_id,)).fetchone()
    if not form or form["source_hash"] != source["sourceIdentityHash"]:
        raise ValueError("The selected formalization source changed. Refresh and send again.")
    if bind_source:
        bind(conn, dict(form), source)
    # Historical snapshots remain readable for accepted work, but UI admission
    # uses immutable publications. Never inspect the mirror in a request.
    return source


def _check_mirror(conn, form: dict, expected: dict, proofs_root=None) -> None:
    from . import uploads
    from .config import load_config
    from pathlib import Path
    project = conn.execute("select * from projects where id = ?", (form.get("project_id"),)).fetchone()
    if not project:
        raise ValueError("The Overleaf project for this source is unavailable.")
    lea_root = load_config().lea_root if proofs_root is None else None
    root = proofs_root or (lea_root or Path(__file__).resolve().parents[2] / "prover") / "workspace" / "proofs"
    base = uploads.overleaf_dir(dict(project), root)
    # Hash exact UTF-8 bytes: read_text's newline conversion would falsely reject
    # a valid archive containing CRLF sources.
    actual = {path.relative_to(base).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in base.rglob("*") if path.is_file() and path.suffix.lower() in uploads.OVERLEAF_SOURCE_EXTS}
    if actual != expected:
        raise ValueError("Overleaf source changed before execution. Refresh and send again.")


def validate_run(run_id: str, proofs_root) -> None:
    """Recheck at dispatch without expiring an already admitted queue entry."""
    from .source_publications import materialize_run
    if materialize_run(run_id,proofs_root):return
    with db.connect() as conn:
        run = conn.execute("select * from runs where id = ?", (run_id,)).fetchone()
        if not run or not run["source_context_ref"]:
            return
        snapshot = conn.execute("select * from overleaf_source_requests where id = ?", (run["source_context_ref"],)).fetchone()
        form = conn.execute("select * from formalizations where id = ?", (run["focus_formalization_id"],)).fetchone()
        if not snapshot or not form:
            raise ValueError("The Overleaf source binding was removed before execution.")
        source = json.loads(snapshot["source_bundle_json"])
        if form["source_hash"] != source["sourceIdentityHash"]:
            raise ValueError("Overleaf source changed before execution. Refresh and send again.")
        latest = conn.execute("select source_bundle_json from overleaf_source_requests where formalization_id = ? and state = 'ready' order by captured_at desc limit 1", (form["id"],)).fetchone()
        if latest and json.loads(latest[0])["bundleHash"] != source["bundleHash"]:
            raise ValueError("Overleaf source changed before execution. Refresh and send again.")
        if snapshot["mirror_files_json"]:
            _check_mirror(conn, dict(form), json.loads(snapshot["mirror_files_json"]), proofs_root)


async def prepare(formalization_id: str) -> dict:
    from .source_publications import latest
    return latest(formalization_id)
