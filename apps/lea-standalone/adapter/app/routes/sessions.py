"""Session + stats endpoints.

The canvas read path used to live here: the DB stored each code_step as a git
*pointer* (commit_sha + path), so on reload the route **hydrated** every step by
shelling out to `git show <sha>:<path>`. As of v2.3 SQL owns the content (C1/D7
inverted) and `store.session_detail` returns each step's bytes directly, so there
is no hydrate step and no composition over a second store — a read that can't
half-succeed. A failed hydrate used to degrade to `code: ""`, i.e. a proof
silently rendering as an empty canvas.
"""

from __future__ import annotations

from .. import peripheral_views

import asyncio
import json
import logging
import re
from queue import Queue
from pathlib import Path

from ..core_query_dispatch import database_query
from .. import workspace_commands
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from lea import condenser
from lea.interface import check as interface_check, rebuild as interface_rebuild, verify as interface_verify

from ..artifacts import classify_lean_artifact
from ..config import load_config
from .. import formalizations as formalization_service
from .. import filesystem as fs_service, netguard, projects, store, project_coordination, proof_files


def _session_project(arguments):
    session = store.get_session(arguments["session_id"])
    return session.get("project_id") if session else None


router = APIRouter()
logger = logging.getLogger("lea-interface.sessions")


class PathRequest(BaseModel):
    file_revision: str | None = None
    # which file in the session to act on; defaults to the latest code_step's path
    path: str | None = None
    # Optional: attribute this check as its own new code_step instead of
    # back-filling the existing latest one. Used by the Overleaf lean pane's
    # cascade re-verification (docs/FEATURE-overleaf-lean-pane-manual-edit.md):
    # when a manual edit to one declaration may have broken another target
    # that imports it, the companion re-checks the *unchanged* dependent file
    # and wants that verdict recorded as its own timeline entry -- 'cascade',
    # a third convention value alongside code_steps.author's existing
    # 'agent' | 'user' (D9, apps/lea-standalone/design/v2-architecture.md).
    # Omitted (the default) preserves the original back-fill-only behavior
    # exactly, so the standalone UI's existing calls are unaffected.
    author: str | None = None
    summary: str | None = None
    formalization_id: str | None = None


class FileWriteRequest(workspace_commands.EditRequest):
    path: str
    content: str
    note: str | None = None  # optional explanation of the edit (D11)
    formalization_id: str | None = None
    # Optional optimistic-concurrency token from
    # GET /api/formalizations/{id}/current. Old clients may omit it.
    base_revision: str | None = None
    check_and_cascade: bool = False


class SessionUpdate(BaseModel):
    title: str


def _validated_formalization_id(
    session_id: str, formalization_id: str | None
) -> str | None:
    if not formalization_id:
        return None
    formalization = store.get_formalization(formalization_id)
    if not formalization:
        raise HTTPException(status_code=404, detail="Formalization not found")
    if session_id not in store.session_ids_for_formalization(formalization_id):
        raise HTTPException(
            status_code=409,
            detail="Formalization is not associated with this session",
        )
    return formalization_id


@router.get("/api/sessions")
@database_query('sessions')
def list_sessions() -> dict:
    from .. import db, observation
    with db.read_snapshot() as conn:
        cursor = observation.watermark(conn)
        return {"sessions": store.list_sessions(), "cursor": cursor}


_SESSIONS_POLL_SECONDS = 1.0
_SESSIONS_MAX_TICKS = 10800


@router.get("/api/sessions/events")
async def session_list_events(request: Request) -> StreamingResponse:
    """One durable invalidation feed; query snapshots own the list contents.

    Resume a consistent list snapshot via ?after=N or Last-Event-ID. Expiry is
    an explicit refresh event, so EventSource never loops on an unreadable 409.
    No process token, digest, worker invocation or open DB transaction spans IO.
    """
    from starlette.concurrency import run_in_threadpool
    from .. import observation
    raw = request.headers.get('last-event-id') or request.query_params.get('after')
    initial = raw is None
    cursor = int(raw) if raw and raw.isascii() and raw.isdigit() and len(raw) <= 19 else 0

    async def stream():
        nonlocal cursor, initial
        for _ in range(_SESSIONS_MAX_TICKS):
            if getattr(request.app.state, 'shutting_down', False) or await request.is_disconnected():
                return
            try:
                page = await run_in_threadpool(observation.changes, cursor)
                next_cursor = page['watermark'] if initial else page['cursor']
                noise={'peripheral_views','commands','command_workers','worker_process','command_attempts','companion_documents',
                       'companion_jobs','companion_associations','download_artifacts','source_publishers'}
                changed = initial or any(change['entity_type'] not in noise for change in page['changes'])
                reset = False
            except observation.CursorReset as exc:
                next_cursor, changed, reset = exc.watermark, True, True
                page = {'has_more': False}
            cursor, initial = next_cursor, False
            if changed:
                payload = {'cursor': cursor, 'reset': reset}
                yield f"event: sessions_changed\nid: {cursor}\ndata: {json.dumps(payload)}\n\n"
            else:
                yield ': keep-alive\n\n'
            if not page['has_more']:
                await asyncio.sleep(_SESSIONS_POLL_SECONDS)
    return StreamingResponse(stream(), media_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@router.get("/api/stats")
@database_query('stats')
def stats(include_proof_status: bool = True) -> dict:
    return store.usage_stats(include_proof_status=include_proof_status)


@router.get("/api/sessions/{session_id}")
@database_query('session')
@project_coordination.guarded(_session_project, metadata=True)
def session_detail(session_id: str) -> dict:
    detail = store.session_detail(session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Session not found")
    items = formalization_service.for_session(session_id)
    focused_runs = [
        run for run in (detail.get("runs") or [])
        if run.get("focus_formalization_id")
    ]
    from ..db import connect
    with connect() as conn:
        queued = [{'id': r[0]} for r in conn.execute("select id from reformalization_operations where session_id=? and (state='queued' or (state='active' and (root_run_id is null or json_extract(result_json,'$.continuation') is not null)))", (session_id,))]
    return {
        **detail,
        "formalizations": items,
        'queued_reformalizations': queued,
        "formalization_summary": formalization_service.summary(items),
        "lean_check_runtime": lean_check_runtime(session_id),
        "latest_focus_formalization_id": (
            focused_runs[-1]["focus_formalization_id"] if focused_runs else None
        ),
    }


@router.patch("/api/sessions/{session_id}")
def update_session(session_id: str, request: SessionUpdate) -> dict:
    title = request.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    updated = store.update_session_title(session_id, title)
    if updated is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return updated


@router.post('/api/sessions/{session_id}/compact', status_code=202)
@workspace_commands.register('session.compact')
def compact_session(session_id: str) -> dict:
    """Manual context compaction (G3) — the user-triggered twin of G1's automatic
    condenser (`/compact` slash command). Runs the SAME condenser on the session's
    stored transcript (the base the next activation replays): prunes superseded tool
    outputs and force-folds the older middle into a summary, then persists the condensed
    transcript back onto its run so the next follow-up starts lighter. Returns the token
    delta for the composer's 'freed ~N tokens' note. A no-op (nothing finished yet, or
    already compact) is reported as `changed: false`, not an error."""
    # Compact between turns, never mid-run: an active run owns its own in-memory messages,
    # so rewriting the stored base underneath it would race.
    if store.has_active_run(session_id):
        raise HTTPException(status_code=409, detail="A run is active — compact after it finishes.")
    latest = store.latest_transcript_run_for_session(session_id)
    if not latest or not latest.get("messages"):
        return {"manual": True, "changed": False, "pruned": 0, "summarized": False,
                "before_tokens": 0, "after_tokens": 0, "freed_tokens": 0,
                "referenced_files": [], "message": None}

    config = load_config()
    messages = latest["messages"]
    model = latest.get("model") or config.model
    before = condenser.estimate_tokens(messages)
    action = store.begin_auxiliary_invocation(latest["run_id"], "manual_compaction", model)
    result = condenser.condense(messages, config, model=model,
                                last_input_tokens=before, force=True)
    store.finish_auxiliary_invocation(action, result.usage, result.cost)
    payload = {
        "manual": True,
        "changed": result.changed,
        "pruned": result.pruned,
        "summarized": bool(result.summarized),
        "before_tokens": result.before_tokens,
        "after_tokens": result.after_tokens,
        "freed_tokens": max(0, result.before_tokens - result.after_tokens),
        # What the model still has in view after compaction — the Claude-Code-style
        # "still referenced" list the /compact surface shows.
        "referenced_files": condenser.referenced_files(result.messages),
    }
    message = None
    if result.changed:
        store.set_run_transcript(latest["run_id"], result.messages)
        # Persist the marker as a durable timeline message (kind='compaction', content =
        # this JSON payload) so it survives a reload — the same channel edit_notes ride.
        # A no-op (nothing to free) is NOT persisted: the transient client notice is
        # correct, since nothing about the session actually changed.
        message = store.add_message(
            session_id, "assistant", json.dumps(payload), latest["run_id"], kind="compaction")
    return {**payload, "message": message}


@router.post('/api/sessions/{session_id}/file', status_code=202)
@workspace_commands.register('session.save')
@project_coordination.guarded(_session_project, write=True)
def write_file_session(session_id: str, request: FileWriteRequest) -> dict:
    """Save human edits in SQL, then atomically materialize their working file.

    Coalesce only unchecked edits; preserve checked and verified history.
    """
    config = load_config()
    if config.lea_root is None:
        raise HTTPException(status_code=422, detail="lea_root is not configured")
    if not request.path:
        raise HTTPException(status_code=400, detail="path is required")

    # Modal lock (D62): the agent and the human never write the same file at once.
    # The live editor already goes read-only during a run; this is the server-side
    # backstop so a stray/racing write is refused rather than clobbering agent state.
    if store.has_active_run(session_id):
        raise HTTPException(status_code=409, detail="A run is active — editing is locked until it finishes.")
    session = store.get_session(session_id)
    if session and session.get("project_id") and store.project_has_active_import(session["project_id"]):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "project_busy",
                "message": "Wait for the active GitHub import before editing project files.",
            },
        )

    resolved = projects.resolve_git(session_id, config.lea_root / "workspace" / "proofs")
    if resolved is None:
        raise HTTPException(status_code=404, detail="Session not found")
    gs, repo_key = resolved  # D24: project session → the shared repo; loose → its own
    repo = gs.init_session(repo_key)
    try:
        absolute = fs_service.safe_abs(repo, request.path)
    except fs_service.FilesystemError as exc:
        raise HTTPException(400, detail=str(exc)) from None
    rel = absolute.relative_to(repo.resolve()).as_posix()
    formalization_id = _validated_formalization_id(session_id, request.formalization_id)
    return proof_files.save(session_id, rel, absolute, request.content,
        formalization_id=formalization_id, base_revision=request.base_revision, note=request.note)


@router.get("/api/sessions/{session_id}/lean-check-runtime")
@peripheral_views.query('lean_check_runtime')
def lean_check_runtime(session_id: str) -> dict:
    """Read current shared-server health without starting Lean or creating files."""
    from lea.lsp_daemon import runtime_snapshot
    from lea.tools import _find_lake_root
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    config = load_config()
    if config.lea_root is None:
        return {"state": "unavailable"}
    project = store.get_project(session["project_id"]) if session.get("project_id") else None
    repo = projects.repo_for_session(session, config.lea_root / "workspace" / "proofs", project)
    return runtime_snapshot(_find_lake_root(str(repo / "Runtime.lean")))


@router.post('/api/sessions/{session_id}/edit',status_code=202)
@workspace_commands.register('session.edit')
def edit_session(session_id: str, request: FileWriteRequest) -> dict:
    from ..workspace_edits import apply
    return apply(session_id,request)


@router.post('/api/sessions/{session_id}/lean-check', status_code=202)
@workspace_commands.register('session.check')
@project_coordination.guarded(_session_project, write=True)
def lean_check_session(session_id: str, request: PathRequest) -> dict:
    from .. import runtime
    try:
        with runtime.operation():
            return _lean_check_session(session_id, request)
    except RuntimeError as exc:
        if str(exc) == "adapter_draining":
            raise HTTPException(status_code=503, detail={"error": "adapter_draining"},
                                headers={"Retry-After": "5"}) from None
        raise


def _lean_check_session(session_id: str, request: PathRequest) -> dict:
    """Standalone `lean_check` on a session's working file (LSP fast path, no run,
    D2). Back-fills the verdict onto that file's latest code_step so the canvas +
    derived session status reflect it.

    When `request.author` is set (e.g. `"cascade"`), the verdict is recorded as
    a *new* code_step instead of back-filling the latest one -- the file on
    disk hasn't changed, so the new step holds the same content as the latest
    step (one blob, by content address); it exists to give the re-check its own
    timeline entry
    (who/why/when), not new content. See the `PathRequest.author` docstring.

    A `"cascade"` check always runs immediately after a `POST .../rebuild` of
    some *other* module in the project (the Overleaf lean pane's manual-edit
    flow, docs/FEATURE-overleaf-lean-pane-manual-edit.md, "Cascade
    verification"). It deliberately still goes through the normal (warm) path
    here, NOT `interface_check(..., cold=True)`: a live end-to-end test found
    the cold subprocess path (`tools.lean_check_cold`, `lake env lean <file>`
    one-shot) did NOT reliably see a just-rebuilt project-local module's fresh
    `.olean` either -- a real Lean/Lake quirk still under investigation, not
    something to build correctness on. Trusting the warm path here instead
    works, and was confirmed by that same test: `rebuild_session_module`
    (below) calls `lsp_daemon.mark_stale` on every successful build, which
    runs strictly before this call in the cascade's own request order, so the
    daemon has already restarted (discarding whatever it had cached for the
    rebuilt module) by the time this check reaches it.
    """
    abs_path, rel = _resolve_proof_path(session_id, request.path)
    formalization_id = _validated_formalization_id(
        session_id, request.formalization_id
    )
    from lea import diagnostics
    from ..bridge import diagnose
    recorded = []
    def record_diagnostic(event):
        recorded.append(diagnose(Queue(), session_id, None, event.severity, event.code,
                                 event.message, source=event.source, remedy=event.remedy,
                                 **{**event.context, "path": rel}))
    token = diagnostics.begin_scope(sink=record_diagnostic)
    try:
        response = proof_files.check(session_id, rel, Path(abs_path), interface_check,
            formalization_id=formalization_id, author=request.author, summary=request.summary)
    finally:
        diagnostics.end_scope(token)
    return {**response, "diagnostics": recorded,
            "lean_check_runtime": lean_check_runtime(session_id)}


@router.post('/api/sessions/{session_id}/rebuild', status_code=202)
@workspace_commands.register('session.rebuild')
@project_coordination.guarded(_session_project, write=True)
def rebuild_session_module(session_id: str, request: PathRequest) -> dict:
    """Force a real `lake build` of a session's working file's module (D2-adjacent
    standalone capability, no agent run), so any *other* session's file that
    `import`s it resolves against a fresh `.olean` instead of a stale one.

    Why this exists: `lean-check` above (and `check_via_lsp`/`lea/lsp_daemon.py`
    it delegates to) is fast precisely because it never touches this file's
    compiled `.olean` -- fine when a session is only ever checking its own file,
    but a project-mate session's `lean-check` of a *dependent* file silently
    resolves its `import` against whatever `.olean` was last built, indefinitely,
    no matter how many times either file is rechecked. The Overleaf lean pane's
    manual-edit cascade (docs/FEATURE-overleaf-lean-pane-manual-edit.md, "Cascade
    verification") calls this once per edited module, before re-checking any of
    its dependents, so a dependent's "still valid" is never a caching artifact.
    """
    abs_path, rel = _resolve_proof_path(session_id, request.path)
    try:
        result = interface_rebuild(abs_path)
    except Exception as exc:  # noqa: BLE001 -- an unexpected failure here must
        # surface as "can't verify" (the companion's cascade already treats a
        # non-2xx/non-"ok" response this way), never as a raw 500 that leaves
        # the caller unable to distinguish "the module doesn't compile" from
        # "the rebuild itself crashed." Whatever this is, it's genuinely a
        # verdict of "unknown," not silent success.
        logger.exception("rebuild failed unexpectedly for session=%s path=%s", session_id, abs_path)
        return {"path": rel, "status": "error", "detail": f"rebuild crashed: {exc}"}
    return {"path": rel, "status": result.status, "detail": result.detail}


@router.post('/api/sessions/{session_id}/verify', status_code=202)
@workspace_commands.register('session.verify')
@project_coordination.guarded(_session_project, write=True)
def verify_session(session_id: str, request: PathRequest) -> dict:
    """Standalone SafeVerify on a session's working file (kernel replay + axiom
    audit, no run, D2). status: ok | rejected | error | unavailable."""
    abs_path, rel = _resolve_proof_path(session_id, request.path)
    formalization_id = _validated_formalization_id(
        session_id, request.formalization_id
    )
    return proof_files.verify(session_id, rel, Path(abs_path), interface_verify,
                              formalization_id=formalization_id)


@router.get("/api/sessions/{session_id}/lsp-info")
def lsp_info(session_id: str, path: str | None = None) -> dict:
    """Legacy initial-content alias. Reads only the published SQL file."""
    from .editor import invoke
    from ..editor_service import file_info
    return invoke(file_info,session_id,path)


@router.websocket("/api/sessions/{session_id}/lsp")
async def lsp_socket(websocket: WebSocket, session_id: str) -> None:
    """Retired execution channel. Every client must use persisted requests."""
    await websocket.close(code=1008,reason="Direct LSP retired; use queued editor requests")


@router.get("/api/sessions/{session_id}/export")
@peripheral_views.query('export_session',automatic=False,lifetime=86400)
def export_session(session_id: str) -> Response:
    """Stream a zip of a session's files (#14). A loose session has its own repo
    (``proofs/<session-id>``) with no other download path — this fills that gap,
    reusing the project export's zip plumbing (source + ``.lea`` assets; ``.git``/
    ``.lake`` excluded). A project session resolves to its shared project repo (the
    project's own Export is the primary path; this stays consistent). 404 if the
    session is unknown or has written no files yet."""
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    config = load_config()
    if config.lea_root is None:
        raise HTTPException(status_code=422, detail="lea_root is not configured")
    proofs_root = config.lea_root / "workspace" / "proofs"
    project = store.get_project(session["project_id"]) if session.get("project_id") else None
    repo = projects.repo_for_session(session, proofs_root, project)
    if not repo.exists():
        raise HTTPException(status_code=404, detail="This session has no files to download yet.")
    try:data = fs_service.export_zip(repo)
    except fs_service.FilesystemError as exc:
        raise HTTPException({'too_large':413,'changed':409,'bad_path':400}.get(exc.code,400),str(exc)) from exc
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": peripheral_views.disposition(_session_zip_name(session))},
    )


def _session_zip_name(session: dict) -> str:
    """A friendly zip filename from the session title, e.g. 'Prove √2 irrational' →
    'prove-2-irrational.zip'. Falls back to the session id when the title is empty
    or slugifies to nothing."""
    slug = re.sub(r"[^a-z0-9]+", "-", (session.get("title") or "").lower()).strip("-")[:60]
    return f"{slug or 'session-' + str(session['id'])[:8]}.zip"


@router.post('/api/sessions/{session_id}/export',status_code=202)
@workspace_commands.register('session.export')
def prepare_session_export(session_id:str):
    return peripheral_views.capture('export_session',{'session_id':session_id})


def _resolve_proof_path(session_id: str, path: str | None) -> tuple[str, str]:
    """(absolute on-disk path, repo-relative path) for the file to check/verify.

    Defaults to the session's latest code_step path. Filesystem-canonical (D3): the
    on-disk file is what the agent, the user, and lean_check/SafeVerify all touch.

    The path is **confined to the session's repo** (AUDIT-2026-07-24 S3). It used to
    be a bare ``repo / rel`` join on a caller-supplied string, so
    ``{"path": "../../../../etc/passwd"}`` escaped the session directory — and all
    three callers act on whatever it resolves to: ``lean-check`` runs Lean over it
    and returns the diagnostics (which quote source lines), ``rebuild`` runs
    ``lake build`` against it, and ``verify`` runs SafeVerify. ``write_file_session``
    has had this guard from the start; this is the same one, via the now-shared
    ``filesystem.safe_abs``, which also keeps ``.git``/``.lake`` internals out of reach.

    The returned relative path is normalized (resolved, POSIX) — the same form
    ``bridge._relativize`` stores — so the code-step lookups keyed on it are
    unaffected."""
    config = load_config()
    if config.lea_root is None:
        raise HTTPException(status_code=422, detail="lea_root is not configured")
    rel = path or _latest_proof_path(session_id)
    if not rel:
        raise HTTPException(status_code=404, detail="No proof file in this session yet")
    resolved = projects.resolve_git(session_id, config.lea_root / "workspace" / "proofs")
    if resolved is None:
        raise HTTPException(status_code=404, detail="Session not found")
    gs, repo_key = resolved
    repo = gs.session_repo(repo_key)
    try:
        abs_path = fs_service.safe_abs(repo, rel)
    except fs_service.FilesystemError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return str(abs_path), abs_path.relative_to(repo.resolve()).as_posix()


def _latest_proof_path(session_id: str) -> str | None:
    detail = store.session_detail(session_id)
    steps = (detail or {}).get("code_steps") or []
    return steps[-1]["path"] if steps else None
