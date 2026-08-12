"""Run endpoints: enqueue, observe over SSE, interrupt, and approve tools.

POST enqueues into the bridge's capacity-aware FIFO dispatcher. GET /events is
a pure observer: pending runs announce their queue position, live runs replay
and tail their rejoinable broker, and terminal runs synthesize ``done`` when the
in-memory broker has already been retired.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import load_config, permission_tier
from .. import bridge
from .. import diagnostics
from .. import formalizations as formalization_service
from .. import github_import_service
from ..bridge import request_stop, request_subagent_stop
from .. import projects
from .. import runbroker
from .. import runregistry
from .. import settings as settings_service
from .. import store

router = APIRouter()


class NewFormalizationRequest(BaseModel):
    display_title: str
    kind: str = "theorem"
    declaration_name: str | None = None
    statement: str | None = None
    origin: str = "ui"
    origin_key: str | None = None
    source_hash: str | None = None


class RunRequest(BaseModel):
    message: str
    session_id: str | None = None
    # The interactive picker sends the model explicitly so each run snapshots the
    # user's choice. Clients that omit it (including older Overleaf companions)
    # inherit the persisted adapter default.
    model: str | None = None
    # Autonomous run (D19): when true the run uses no per-tool approval gate and the
    # non-interactive `default` prompt variant, so it formalizes end-to-end with zero
    # human interaction (the Overleaf path). Defaults false → the interactive UI
    # behavior (gated tools + collaborator prompt) is unchanged.
    autonomous: bool = False
    # Project namespace (the Overleaf document slug). When present, a new session is
    # tagged with a project of this slug (created on first use) so per-document usage
    # can be aggregated for the Overleaf popover's "This project" total. Absent for
    # the interactive UI path, which stays project-less.
    project_slug: str | None = None
    project_title: str | None = None
    project_namespace: str | None = None
    # Session origin / providence. 'overleaf' (with `origin_url` = the canonical
    # Overleaf document URL) marks a formalization spawned from the Overleaf
    # extension, so the UI can show an origin indicator and open/focus the source
    # document. Omitted for the interactive UI path → the session defaults to 'ui'.
    # Independent of `project_slug` (usage namespacing) by design.
    origin: str | None = None
    origin_url: str | None = None
    focus_formalization_id: str | None = None
    focus_source_hash: str | None = None
    new_formalization: NewFormalizationRequest | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str


def sse(event_type: str, payload: dict, seq: int | None = None) -> str:
    # A monotonic `id:` lets the browser's native EventSource reconnect resume via
    # `Last-Event-ID` (no re-replay). Manual reattach omits it and replays from 0.
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _request_cursor(request: Request) -> int:
    """Where to resume the broker stream from.

    Native EventSource reconnects send ``Last-Event-ID``; manual reattach may
    pass ``?since=N``. Otherwise replay the broker from its first event.
    """
    last = request.headers.get("last-event-id")
    if last and last.isdigit():
        return int(last)
    since = request.query_params.get("since")
    return int(since) if since and since.isdigit() else 0


def _done_payload(run: dict) -> dict:
    payload = {"status": run["status"]}
    if run.get("result_kind"):
        payload["result_kind"] = run["result_kind"]
    if run.get("result_detail"):
        payload["result_detail"] = run["result_detail"]
    return payload


async def _subscribe(broker: runbroker.RunBroker, cursor: int):
    """Replay after ``cursor`` and follow a live broker through ``done``."""
    while True:
        pending = broker.events_after(cursor)
        for event in pending:
            cursor = event["seq"]
            yield sse(event["type"], event["payload"], seq=event["seq"])
            if event["type"] == "done":
                return
        if broker.closed and not broker.events_after(cursor):
            return
        await asyncio.sleep(0.08)


async def _passive_done(run_id: str):
    """Settle an observer when the broker was retired just before attachment."""
    for _ in range(36000):  # ~3h at 0.3s/iter; the client may reconnect sooner
        current = store.get_run(run_id)
        if not current:
            yield sse("done", {"status": "failed"})
            return
        if current["status"] not in {"pending", "running"}:
            yield sse("done", _done_payload(current))
            return
        await asyncio.sleep(0.3)
    yield sse("done", {"status": "running"})


@router.post("/api/runs")
def create_run(request: RunRequest) -> dict:
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    config = load_config()
    if settings_service.spend_limit_reached(config.max_spend_usd):
        raise HTTPException(status_code=402, detail="Max spend limit has been reached.")
    selected_model = config.model
    if request.model is not None:
        try:
            selected_model = settings_service.validate_configured_model(request.model)
        except settings_service.SettingsValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "field": exc.field},
            ) from exc

    project_id: str | None = None
    # B2: why the project didn't attach, if it didn't. Recorded here and persisted
    # once the session exists (below) — a run silently losing its project context,
    # instructions, skills, and shared repo used to look identical to a run that
    # never asked for one.
    project_error: str | None = None
    if request.project_slug:
        proofs_root = (config.lea_root / "workspace" / "proofs") if config.lea_root else None
        try:
            project = projects.ensure_project(
                request.project_slug,
                proofs_root,
                title=request.project_title,
                namespace=request.project_namespace,
            )
            project_id = project["id"]
        except ValueError as exc:
            project_id = None
            project_error = str(exc) or "the project slug was rejected"
    if project_id is None and request.session_id:
        existing_session = store.get_session(request.session_id)
        project_id = existing_session.get("project_id") if existing_session else None
    if project_id and store.project_has_active_import(project_id):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "project_busy",
                "message": "Wait for the active GitHub import before starting a Lea run.",
            },
        )

    autonomous = request.autonomous or (permission_tier() == "none")
    new_formalization = (
        request.new_formalization.model_dump()
        if request.new_formalization is not None else None
    )
    focus_source_hash = request.focus_source_hash
    if not focus_source_hash and new_formalization:
        focus_source_hash = new_formalization.get("source_hash")
    try:
        bundle = store.create_run_bundle(
            message=message,
            session_id=request.session_id,
            project_id=project_id,
            session_origin=(request.origin or "ui"),
            session_origin_url=request.origin_url,
            model=selected_model,
            provider=None,
            max_turns=config.max_turns,
            autonomous=autonomous,
            focus_formalization_id=request.focus_formalization_id,
            focus_source_hash=focus_source_hash,
            new_formalization=new_formalization,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session = bundle["session"]
    run = bundle["run"]
    user_message = bundle["message"]
    project_id = session.get("project_id")
    if project_error and not project_id:
        # B2: persisted, not streamed — this endpoint returns before any SSE stream is
        # attached, so there is no live channel yet. The client picks it up from
        # `session_detail` on attach, which is also how it survives a reload. Recorded
        # here rather than at the failure site because it needs the run row the bundle
        # just created.
        store.add_diagnostic(session["id"], run["id"], diagnostics.resolve(
            "degraded", "run.project_unavailable",
            f"This run could not be attached to project '{request.project_slug}' "
            f"({project_error}); it runs without project context, instructions, or skills.",
            source="runs",
            context={"project_slug": request.project_slug},
        ))
    raw_formalization = bundle.get("formalization")
    if raw_formalization is not None and project_id and config.lea_root:
        project_for_adoption = store.get_project(project_id)
        if project_for_adoption:
            github_import_service.try_adopt_imported_declaration(
                project_for_adoption,
                raw_formalization,
                config.lea_root / "workspace" / "proofs",
            )
    bridge.enqueue_run(run["id"])
    formalization = (
        formalization_service.decorate([raw_formalization])[0]
        if raw_formalization is not None
        else None
    )
    project = store.get_project(project_id) if project_id else None
    return {
        "session_id": session["id"],
        "run_id": run["id"],
        "model": selected_model,
        "message": user_message,
        "focus_formalization_id": run.get("focus_formalization_id"),
        "formalization": formalization,
        "project_id": project_id,
        "project_slug": project["slug"] if project else None,
        "project_namespace": project["namespace"] if project else None,
        "queue_position": store.queue_position(run["id"]),
    }


@router.get("/api/runs/{run_id}")
def get_run_row(run_id: str) -> dict:
    """Return the cheap lifecycle/result columns used by reconnect polling."""
    row = store.get_run_status(run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    return row


@router.post("/api/runs/{run_id}/approvals/{approval_id}")
def resolve_approval(run_id: str, approval_id: str, request: ApprovalDecisionRequest) -> dict:
    if request.decision not in {"allow", "deny", "always_session"}:
        raise HTTPException(
            status_code=422,
            detail="decision must be 'allow', 'deny', or 'always_session'",
        )
    if not bridge.resolve_approval(run_id, approval_id, request.decision):
        raise HTTPException(
            status_code=409,
            detail="No pending approval matches this run/approval id",
        )
    return {"status": "resolved", "decision": request.decision}


@router.post("/api/runs/{run_id}/interrupt")
def interrupt_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] not in {"pending", "running"}:
        raise HTTPException(status_code=409, detail="Run is not active")
    request_stop(run_id)
    # A queued run has no driver to read the stop flag, so the endpoint finalizes it
    # itself — atomically (AUDIT-2026-07-24 C7). This used to read the status, ask the
    # registry whether the run was active, and then write, which the dispatcher could
    # interleave with: the run got marked failed and then started anyway, after the
    # client had been told it was interrupted. `fail_pending_run` and
    # `store.claim_pending_run` (in `run_lea`) are the same conditional UPDATE from
    # opposite sides, so exactly one of them can win.
    if store.fail_pending_run(run_id, "Interrupted before the run started."):
        bridge.publish_terminal_from_row(run_id)
        return {"status": "interrupted"}
    return {"status": "interrupting"}


@router.post("/api/sub-agents/{session_id}/interrupt")
def interrupt_subagent(session_id: str) -> dict:
    """Stop a single running child sub-agent (D2), addressed by its child SESSION id —
    without cancelling the coordinator run that spawned it. The child returns its partial
    findings at its next turn boundary and the coordinator carries on. A no-op (404) if
    the child is not currently running in-process (nothing to signal)."""
    if request_subagent_stop(session_id):
        return {"status": "interrupting"}
    raise HTTPException(status_code=404, detail="No running sub-agent for that session")


@router.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """Observe a queued, live, or terminal run without affecting admission."""
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    cursor = _request_cursor(request)
    broker = runbroker.get(run_id)
    if broker is None:
        # Terminal rows normally land here because run_lea retires the broker.
        # The same fallback also closes the tiny running/drop race cleanly.
        return StreamingResponse(_passive_done(run_id), media_type="text/event-stream")

    queue_position = store.queue_position(run_id)

    async def stream_events():
        if queue_position is not None and cursor == 0:
            yield sse("queued", {"run_id": run_id, "position": queue_position})
        async for frame in _subscribe(broker, cursor):
            yield frame

    return StreamingResponse(stream_events(), media_type="text/event-stream")
