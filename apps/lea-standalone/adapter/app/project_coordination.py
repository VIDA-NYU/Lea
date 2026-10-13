"""In-process namespace coordination for the single-process adapter.

Reads are optimistic: they may run concurrently, but cannot publish evidence
assembled across a migration. Mutations reserve the project before admission or
disk I/O, so migration cannot pass its active-run check ahead of a new run.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field
from functools import wraps
import inspect
from threading import Condition, Lock
from uuid import uuid4

from fastapi import HTTPException


class ProjectRenaming(HTTPException):
    def __init__(self):
        super().__init__(409, detail={"error": "project_renaming",
                                     "message": "Project identity is changing. Refresh shortly."},
                         headers={"Retry-After": "1"})


def _available(state):
    if state.deleted:
        raise HTTPException(404, detail={"error": "project_deleted", "message": "Project no longer exists."})
    if state.deleting:
        raise HTTPException(409, detail={"error": "project_busy", "message": "Project is being deleted."})
    if state.renaming:
        raise ProjectRenaming()


@dataclass
class _State:
    lock: Lock = field(default_factory=Lock)
    revision: str = field(default_factory=lambda: uuid4().hex)
    renaming: bool = False
    deleting: bool = False
    deleted: bool = False
    mutations: int = 0
    edits: int = 0
    admissions: int = 0
    workers: set[str] = field(default_factory=set)
    condition: Condition = field(init=False)

    def __post_init__(self):
        self.condition = Condition(self.lock)


_states: dict[str, _State] = {}
_states_lock = Lock()


def _state(project_id: str) -> _State:
    with _states_lock:
        if project_id not in _states:
            _states[project_id] = _State()
        return _states[project_id]


def snapshot(project_id: str) -> dict:
    state = _state(project_id)
    with state.lock:
        return {"namespaceRevision": state.revision, "renameInProgress": state.renaming}


@contextmanager
def read(project_id: str | None):
    if not project_id:
        yield None
        return
    state = _state(project_id)
    with state.lock:
        _available(state)
        revision = state.revision
    try:
        yield revision
    finally:
        # Also replace a filesystem exception caused by an overlapping move.
        with state.lock:
            if state.deleted or state.deleting:
                _available(state)
            if state.renaming or state.revision != revision:
                raise ProjectRenaming()


@contextmanager
def mutation(project_id: str | None, *, wait=False, admission=False):
    if not project_id:
        yield
        return
    state = _state(project_id)
    with state.condition:
        while state.renaming:
            if not wait:
                raise ProjectRenaming()
            state.condition.wait()
        _available(state)
        if admission and state.edits:
            raise HTTPException(409, detail={"error": "project_busy", "message": "A project file is being saved. Retry shortly."})
        state.mutations += 1
        if admission:
            state.admissions += 1
        revision = state.revision
    try:
        yield revision
    finally:
        with state.lock:
            state.mutations -= 1
            if admission:
                state.admissions -= 1


@contextmanager
def migration(project_id: str):
    state = _state(project_id)
    with state.lock:
        _available(state)
        if state.mutations or state.workers:
            raise HTTPException(409, detail={"error": "project_busy",
                                            "message": "Wait for the current project operation before renaming."})
        state.renaming = True
        state.revision = uuid4().hex
    try:
        yield
    finally:
        with state.condition:
            state.renaming = False
            state.revision = uuid4().hex
            state.condition.notify_all()


def worker_started(project_id: str | None, run_id: str):
    if project_id:
        state = _state(project_id)
        with state.lock:
            _available(state)
            state.workers.add(run_id)


def worker_finished(project_id: str | None, run_id: str):
    if project_id:
        state = _state(project_id)
        with state.lock:
            state.workers.discard(run_id)


@contextmanager
def deletion(project_id: str):
    """Exclude admission and I/O for the full busy check and destructive operation."""
    state = _state(project_id)
    with state.lock:
        _available(state)
        if state.mutations or state.workers:
            raise HTTPException(409, detail={"error": "project_busy",
                                             "message": "Wait for current project work before deleting."})
        state.deleting = True
        state.revision = uuid4().hex
    try:
        yield
    finally:
        with state.condition:
            state.deleting = False
            state.revision = uuid4().hex
            state.condition.notify_all()


def mark_deleted(project_id: str):
    state = _state(project_id)
    with state.lock:
        state.deleted = True


@contextmanager
def file_edit(project_id: str | None):
    """A short save reservation: admission waits until bytes are materialized."""
    if not project_id:
        yield
        return
    state = _state(project_id)
    with state.lock:
        _available(state)
        if state.workers or state.admissions:
            raise HTTPException(409, detail={"error": "project_busy",
                "message": "Wait for active project work before editing files."})
        state.edits += 1
        state.mutations += 1
    try:
        yield
    finally:
        with state.lock:
            state.edits -= 1
            state.mutations -= 1


def guarded(resolver, *, write=False, metadata=False, admission=False, environment_write=True):
    """Guard sync/async route functions without changing FastAPI's signature.

    Database-only mutations can opt out of the compiler environment lease while
    retaining their project reservation. Source and environment writes keep the
    exclusive lease by default.
    """
    def decorate(fn):
        signature = inspect.signature(fn, eval_str=True)

        def context(args, kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            project_id = resolver(bound.arguments)
            @contextmanager
            def coordinated():
                with ExitStack() as stack:
                    if project_id:
                        state = _state(project_id)
                        with state.lock:
                            _available(state)
                    if write and environment_write and fn.__name__ not in {'write_file_session', 'write_project_file'} and not any(word in fn.__name__ for word in ('check', 'verify', 'rebuild')):
                        from lea.check_evidence import mutation as environment_mutation
                        from .config import load_config
                        from pathlib import Path
                        root = load_config().lea_root or Path(__file__).resolve().parents[2] / 'prover'
                        stack.enter_context(environment_mutation(root / 'workspace'))
                    revision = stack.enter_context(mutation(project_id, admission=admission) if write else read(project_id))
                    yield revision
            return project_id, coordinated()

        def finish(result, project_id, revision):
            if metadata and project_id and isinstance(result, dict):
                from . import store
                project = store.get_project(project_id)
                result = {**result, "namespace_revision": revision,
                          "project_namespace": project["namespace"] if project else None}
            return result

        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def wrapper(*args, **kwargs):
                project_id, ctx = context(args, kwargs)
                with ctx as revision:
                    return finish(await fn(*args, **kwargs), project_id, revision)
        else:
            @wraps(fn)
            def wrapper(*args, **kwargs):
                project_id, ctx = context(args, kwargs)
                with ctx as revision:
                    return finish(fn(*args, **kwargs), project_id, revision)
        wrapper.__signature__ = signature
        return wrapper
    return decorate
