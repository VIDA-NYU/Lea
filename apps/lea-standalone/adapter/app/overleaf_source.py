"""Fresh editor snapshots shared by Overleaf and LeaChat, independent of runs.

The companion transports pending requests to the extension. Only the adapter
validates and owns the snapshot; admitted runs retain their own frozen copy.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import db
from .source_context import validate_source

REFRESH_SECONDS = 45
SNAPSHOT_SECONDS = 90


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


def new_request(formalization_id: str) -> str:
    now = datetime.now(timezone.utc)
    request_id = uuid4().hex
    with db.write() as conn:
        # Keep frozen provenance for admitted runs; discard unused expired replies.
        conn.execute("update overleaf_source_requests set state = 'error', error = 'Source refresh expired.' where state = 'pending' and expires_at < ?", (now.isoformat(),))
        conn.execute("delete from overleaf_source_requests where state != 'pending' and expires_at < ? and not exists (select 1 from runs where runs.source_context_ref = overleaf_source_requests.id)", (now.isoformat(),))
        form = conn.execute("select * from formalizations where id = ?", (formalization_id,)).fetchone()
        if not form:
            raise LookupError("Formalization not found")
        bind(conn, dict(form))
        conn.execute("insert into overleaf_source_requests (id, formalization_id, state, created_at, expires_at) values (?, ?, 'pending', ?, ?)",
                     (request_id, formalization_id, now.isoformat(), (now + timedelta(seconds=REFRESH_SECONDS)).isoformat()))
    return request_id


def pending() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("""select r.id as request_id, r.formalization_id,
            b.document_id as overleafProjectId, b.target_kind as targetKind, b.target_key as targetLabel
            from overleaf_source_requests r join overleaf_bindings b using (formalization_id)
            where r.state = 'pending' and r.expires_at > ? order by r.created_at""", (db.utc_now(),))]


def respond(request_id: str, source: dict | None, error: str | None = None, document_id: str | None = None,
            mirrored_files: dict | None = None) -> None:
    value = validate_source(source) if source is not None else None
    with db.write() as conn:
        row = conn.execute("select * from overleaf_source_requests where id = ?", (request_id,)).fetchone()
        if not row or row["expires_at"] <= db.utc_now():
            raise ValueError("Source refresh expired. Send again to request current source.")
        binding = conn.execute("select * from overleaf_bindings where formalization_id = ?", (row["formalization_id"],)).fetchone()
        if not binding:
            raise ValueError("The formalization source binding was removed.")
        if document_id != binding["document_id"]:
            raise ValueError("Source response belongs to a different Overleaf document.")
        if value and (value["targetKind"], value["targetKey"]) != (binding["target_kind"], binding["target_key"]):
            raise ValueError("Source belongs to a different formalization target.")
        form = conn.execute("select * from formalizations where id = ?", (row["formalization_id"],)).fetchone()
        if value and (value.get("mirror") or {}).get("verified"):
            if not mirrored_files:
                raise ValueError("The full Overleaf mirror could not be verified. Synchronize and send again.")
            _check_mirror(conn, dict(form), mirrored_files)
        if row["state"] != "pending":
            if value and row["source_bundle_json"] == json.dumps(value, ensure_ascii=False):
                return
            raise ValueError("Source refresh already settled. Request a new snapshot.")
        now = datetime.now(timezone.utc)
        conn.execute("update overleaf_source_requests set state = ?, source_bundle_json = ?, mirror_files_json = ?, error = ?, captured_at = ?, expires_at = ? where id = ?",
                     ("ready" if value else "error", json.dumps(value, ensure_ascii=False) if value else None,
                      json.dumps(mirrored_files) if value and value.get("mirror") else None,
                      error or (None if value else "Overleaf source is unavailable."), now.isoformat(),
                      (now + timedelta(seconds=SNAPSHOT_SECONDS)).isoformat(), request_id))
        if value:
            conn.execute("update formalizations set source_hash = ?, updated_at = ? where id = ?",
                         (value["sourceIdentityHash"], now.isoformat(), row["formalization_id"]))


def resolve(conn, reference: str, formalization_id: str, *, bind_source: bool = True) -> dict:
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
    if row["mirror_files_json"]:
        _check_mirror(conn, dict(form), json.loads(row["mirror_files_json"]))
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
    request_id = new_request(formalization_id)
    try:
        async with asyncio.timeout(REFRESH_SECONDS):
            while True:
                with db.connect() as conn:
                    row = conn.execute("select * from overleaf_source_requests where id = ?", (request_id,)).fetchone()
                if row and row["state"] == "ready":
                    source = json.loads(row["source_bundle_json"])
                    return dict(source_context_ref=request_id, captured_at=row["captured_at"],
                                source_identity_hash=source["sourceIdentityHash"], source_bundle_hash=source["bundleHash"])
                if not row or row["state"] == "error":
                    raise ValueError(row["error"] if row else "The selected formalization was removed.")
                await asyncio.sleep(0.1)
    except (TimeoutError, asyncio.CancelledError):
        with db.write() as conn:
            conn.execute("update overleaf_source_requests set state = 'error', error = 'Open this document in Overleaf with the extension connected, then send again.' where id = ? and state = 'pending'", (request_id,))
        raise
