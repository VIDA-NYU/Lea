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
from threading import Condition, Lock, RLock, get_ident
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


class ApplicationGate:
    """Concurrent ordinary I/O; exclusive, reentrant application intervals."""
    def __init__(self):
        self.condition = Condition(RLock())
        self.readers = {}
        self.writer = None
        self.depth = 0

    def acquire(self):
        tid = get_ident()
        with self.condition:
            self.condition.wait_for(lambda: self.writer in (None, tid))
            self.readers[tid] = self.readers.get(tid, 0) + 1

    def release(self):
        tid = get_ident()
        with self.condition:
            self.readers[tid] -= 1
            if not self.readers[tid]:
                del self.readers[tid]
            self.condition.notify_all()

    def __enter__(self):
        tid = get_ident()
        with self.condition:
            self.condition.wait_for(lambda: self.writer in (None, tid) and not any(t != tid for t in self.readers))
            self.writer = tid
            self.depth += 1
        return self

    def __exit__(self, *_):
        with self.condition:
            self.depth -= 1
            if not self.depth:
                self.writer = None
            self.condition.notify_all()


@dataclass
class _State:
    lock: Lock = field(default_factory=Lock)
    application_lock: ApplicationGate = field(default_factory=ApplicationGate)
    revision: str = field(default_factory=lambda: uuid4().hex)
    renaming: bool = False
    deleting: bool = False
    deleted: bool = False
    mutations: int = 0
    edits: int = 0
    admissions: int = 0
    reviews: int = 0
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
    state.application_lock.acquire()
    try:
        from .reformalization_intake import assert_readable
        assert_readable(project_id)
        yield revision
    finally:
        state.application_lock.release()
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
    if not admission:
        from .reformalization_intake import assert_writer
        assert_writer(project_id)
    with state.condition:
        while state.renaming:
            if not wait:
                raise ProjectRenaming()
            state.condition.wait()
        _available(state)
        if admission and (state.edits or state.reviews):
            raise HTTPException(409, detail={"error": "project_busy", "message": "A project file is being saved. Retry shortly."})
        state.mutations += 1
        if admission:
            state.admissions += 1
        revision = state.revision
    state.application_lock.acquire()
    try:
        yield revision
    finally:
        with state.lock:
            state.mutations -= 1
            if admission:
                state.admissions -= 1
        state.application_lock.release()


@contextmanager
def application(project_id, root, *, recovery=False):
    """Environment first, then the short gate shared by canonical I/O routes."""
    from lea.check_evidence import mutation as environment_mutation
    from .reformalization_intake import assert_writer
    with environment_mutation(root), _state(project_id).application_lock:
        if not recovery:
            assert_writer(project_id)
        yield


@contextmanager
def migration(project_id: str):
    from .reformalization_intake import assert_writer
    assert_writer(project_id)
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
    from .reformalization_intake import assert_writer
    assert_writer(project_id)
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
    from .reformalization_intake import assert_writer
    assert_writer(project_id)
    with state.lock:
        _available(state)
        if state.workers or state.admissions or state.reviews:
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


def review_busy(project_id: str | None, run_id: str | None) -> bool:
    if not project_id:
        return False
    state = _state(project_id)
    with state.lock:
        return bool(state.edits or state.admissions or state.mutations > state.reviews or run_id in state.workers)


@contextmanager
def review_decision(project_id: str | None, run_id: str | None):
    """Reserve a correspondence decision against admission and unfinished saves."""
    if not project_id:
        yield
        return
    state = _state(project_id)
    with state.lock:
        _available(state)
        if state.edits or state.admissions or state.mutations > state.reviews or run_id in state.workers:
            raise HTTPException(409, detail={"error": "review_busy",
                "message": "Wait for the current target operation to finish before approving."})
        state.reviews += 1
        state.mutations += 1
    try:
        yield
    finally:
        with state.lock:
            state.reviews -= 1
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
                    # Managed dependency workers carry a registered parent identity.
                    # They may satisfy their waiting owner's reservation.
                    from . import reformalization_intake as reformalizations, store
                    req = bound.arguments.get("request")
                    run = store.get_run(bound.arguments.get("run_id")) if bound.arguments.get("run_id") else None
                    op = reformalizations.for_run(run["id"]) if run else None
                    parent = getattr(req, "initiating_run_id", None)
                    if (parent and getattr(req, "purpose", None) == "project_dependency"
                            and reformalizations.registered_dependency(parent, getattr(req, 'dependency_id', None), project_id)):
                        op = reformalizations.for_run(parent)
                    if op and op["project_id"] == project_id:
                        stack.enter_context(reformalizations.acting(op["id"]))
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
                result = {**result, "namespace_revision": project["namespace_generation"] if project else None,
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
