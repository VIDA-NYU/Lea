"""Revision-aware manual proof edits and checks shared by both file editors."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from threading import Lock, RLock

from fastapi import HTTPException

from . import formalizations, project_coordination, store
from .db import write

_locks = {}
_locks_guard = Lock()


@contextmanager
def file_guard(path: Path):
    key = str(path.resolve())
    with _locks_guard:
        lock = _locks.setdefault(key, RLock())
    with lock:
        yield


def conflict():
    raise HTTPException(409, detail={"code": "proof_revision_changed",
        "message": "The proof changed while it was being checked. Refresh the current revision and check again."})


def _session(session_id):
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(404, detail="Session not found")
    return session


def _snapshot(formalization_id, session_id):
    return formalizations.current_snapshot(formalization_id, conversation_session_id=session_id) if formalization_id else None


def _same(left, right):
    return (left is None and right is None) or (left is not None and right is not None and
        left["id"] == right["id"] and left["blob_id"] == right["blob_id"])


def check(session_id, path, absolute: Path, checker, *, formalization_id=None,
          author=None, summary=None, expected=None):
    from .workspace_commands import authorize
    authorize()
    from lea.check_evidence import public_execution, coordinator, lake_root
    session = _session(session_id)
    with project_coordination.mutation(session.get("project_id")):
        with file_guard(absolute):
            step = store.current_proof_step(session_id, path)
            if expected is not None and not _same(expected, step):
                conflict()
            content = absolute.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if step and (step.get("content_lost") or step["code"].encode() != content):
                conflict()
        result = checker(str(absolute), allow_cold=False) if author == "cascade" else checker(str(absolute))
        execution = getattr(result, "execution", None) or {}
        with coordinator(lake_root(absolute) or absolute.parent).lease(), file_guard(absolute):
            authorize()
            if execution.get("content_sha256") != digest:
                conflict()
            try:
                if absolute.read_bytes() != content:
                    conflict()
            except OSError:
                conflict()
            from .proof_evidence import record_check
            persisted, updated = record_check(session_id, path, absolute, result, expected=step,
                require_current=True, content=content.decode(), formalization_id=formalization_id,
                author=author, summary=summary)
            if not persisted:
                conflict()
        response = {"path": path, "status": result.status, "detail": result.detail,
                    "execution": public_execution(getattr(result, "execution", None)),
                    "formalization_id": formalization_id or (step or {}).get("formalization_id")}
        response.update(check_current=bool(updated and updated.get("check_current")),
                        check_stale_reason=updated.get("check_stale_reason") if updated else "missing_evidence")
        if updated:
            response["code_step"] = updated
        return response


def _materialize(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def save(session_id, path, absolute: Path, content: str, *, formalization_id=None,
         base_revision=None, note=None):
    from . import workspace_commands, workspace_files
    if workspace_commands.current() is not None:
        result = workspace_files.save(session_id, path, absolute, content,
            formalization_id=formalization_id, base_revision=base_revision, note=note)
        workspace_files.provenance(result['effect_id'])
        return result
    session = _session(session_id)
    project_id = session.get("project_id")
    if store.has_active_run(session_id) or (project_id and (store.project_has_active_run(project_id) or store.project_has_active_import(project_id))):
        raise HTTPException(409, detail={"error": "project_busy", "message": "Wait for active project work before editing proofs."})
    from lea.check_evidence import mutation
    with mutation(absolute), project_coordination.file_edit(project_id), file_guard(absolute):
        if store.has_active_run(session_id) or (project_id and (
            store.project_has_active_run(project_id) or store.project_has_active_import(project_id))):
            raise HTTPException(409, detail={"error": "project_busy",
                                             "message": "Wait for active project work before editing proofs."})
        snapshot = _snapshot(formalization_id, session_id)
        if base_revision is not None and snapshot and base_revision != snapshot.get("revision_token"):
            raise HTTPException(409, detail={"code": "revision_conflict",
                "message": "This formalization changed in another conversation. Refresh before saving.",
                "current_revision": snapshot.get("revision_token"), "last_updated_session": snapshot.get("last_updated_session")})
        current = store.current_proof_step(session_id, path)
        try:
            disk_matches = absolute.read_bytes() == content.encode()
        except OSError:
            disk_matches = False
        if current and not current.get("content_lost") and current["code"] == content and disk_matches:
            return {"unchanged": True, "code_step": None, "note": None,
                    "revision_token": (snapshot or {}).get("revision_token")}
        step = store.upsert_user_code_step(session_id, path, content=content,
            formalization_id=formalization_id, coalesce=bool(current and current["session_id"] == session_id))
        if formalization_id:
            store.link_formalization_file(formalization_id, path, "generated")
        # A shared-file or dependency edit invalidates legacy per-session verification.
        with write() as conn:
            if project_id:
                conn.execute("update runs set safe_verify_status=NULL,safe_verify_detail=NULL "
                             "where session_id in (select id from sessions where project_id=?)", (project_id,))
            else:
                conn.execute("update runs set safe_verify_status=NULL,safe_verify_detail=NULL where session_id=?", (session_id,))
        note_message = store.add_message(session_id, "user", note.strip(), None,
            kind="edit_note", formalization_id=formalization_id) if note and note.strip() else None
        try:
            _materialize(absolute, content)
        except OSError:
            raise HTTPException(500, detail={"code": "content_saved_materialization_failed",
                "message": "Your edit was saved in history, but its working file could not be written. Refresh and retry.",
                "current_revision": (_snapshot(formalization_id, session_id) or {}).get("revision_token")}) from None
        updated = _snapshot(formalization_id, session_id)
        return {"unchanged": False, "code_step": step, "note": note_message,
                "revision_token": (updated or {}).get("revision_token")}


def verify(session_id, path, absolute: Path, verifier, *, formalization_id=None):
    from .workspace_commands import authorize
    authorize()
    from lea.check_evidence import coordinator, lake_root
    session = _session(session_id)
    with project_coordination.mutation(session.get("project_id")):
        with file_guard(absolute):
            step = store.current_proof_step(session_id, path)
            content = absolute.read_bytes()
            if step and (step.get("content_lost") or step["code"].encode() != content):
                conflict()
        result = verifier(str(absolute))
        with coordinator(lake_root(absolute) or absolute.parent).lease(), file_guard(absolute):
            authorize()
            try:
                if absolute.read_bytes() != content:
                    conflict()
            except OSError:
                conflict()
            formalization_id = formalization_id or (step or {}).get("formalization_id")
            event = store.record_verification_event(session_id=session_id,
                formalization_id=formalization_id, path=path, status=result.status,
                detail=result.detail, code_step_id=(step or {}).get("id"),
                run_id=(step or {}).get("run_id"), expected=step,
                require_current=True, persist_session=True,
                execution=getattr(result, "execution", None), content=content.decode())
            if event is None:
                conflict()
        from .proof_evidence import verification_current
        return {"path": path, "status": result.status, "detail": result.detail,
                "current": verification_current(event, step),
                "formalization_id": formalization_id, "verification_event": event}
